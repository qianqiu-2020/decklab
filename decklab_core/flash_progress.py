"""Bounded preview timings in the existing durable flash task metadata."""
import json
import statistics
import time


class FlashWriteProgress:
    """Map each executor's logical byte counter onto the sealed write sequence."""

    def __init__(self, plan):
        self.total = 0
        self.components = {}
        self.ranges = {}
        self.sample = None
        self.ec_components = set()
        steps = [(step['label'], step['result']['plan']) for step in plan.get('steps', [])]
        for component, native in steps or [('', plan)]:
            start = self.total
            if (native.get('ec') or {}).get('state') == 'selected':
                self.ec_components.add(component)
            for write in native.get('writes', []):
                size = write.get('size')
                if type(size) is not int or size <= 0:
                    self.ranges.clear()
                    return
                key = (component, write.get('storage'), write.get('partition'))
                self.ranges[key] = (self.total - start, size, write.get('target') if write.get('kind') == 'layout' else None)
                self.total += size
            self.components[component] = (start, self.total - start)

    def measure(self, event):
        done, total = event.get('done', 0), event.get('total', 0)
        component = event.get('component', '')
        key = (component, event.get('storage'), event.get('partition'))
        span = self.ranges.get(key)
        base, expected = self.components.get(component, (0, 0))
        if (key[1:] == ('EC', 'EC') and component in self.ec_components
                and event.get('event') == 'phase'):
            self._sample(key, 0, time.monotonic())
            return base, self.total, dict(storage='EC', component=component, partition='EC',
                                          step_started_ms=self.sample[3])
        if not span or total != expected or not span[0] <= done <= span[0] + span[1]:
            return done, total, {}
        count = done - span[0]
        detail = dict(partition=span[2] or key[2], storage=key[1], component=component,
                      partition_done=count, partition_total=span[1])
        now = time.monotonic()
        self._sample(key, count, now)
        detail['step_started_ms'] = self.sample[3]
        elapsed = now - self.sample[2]
        if elapsed >= 0.25 and count > self.sample[1]:
            detail['bytes_per_second'] = round((count - self.sample[1]) / elapsed)
        return base + done, self.total, detail

    def _sample(self, key, count, now):
        if not self.sample or self.sample[0] != key or count < self.sample[1]:
            self.sample = (key, count, now, int(time.time() * 1000))


PHASES = ('checking-upload', 'checking-image-sha256', 'expanding-image',
          'checking-payloads', 'checking-layout', 'reading-payload-metadata')


class PreviewProgress:
    def __init__(self, store, task_id, source, executor, compressed=True, clock=time.monotonic, phases=PHASES):
        self.store, self.task_id, self.clock = store, task_id, clock
        self.phases = phases
        self.start = self.phase_start = clock()
        self.index = -1
        self.size = source['size']
        # Conservative initial costs; use measured stage seconds per input byte
        # from recent successful previews on this executor when available.
        mib = 1024 ** 2
        defaults = [self.size/(150*mib), self.size/(150*mib), self.size/(60*mib),
                    self.size*3.5/(175*mib), 5, 3]
        histories = []
        with store.store.lock:
            rows = store.store.db.execute("SELECT f.document FROM flash_details f JOIN tasks t ON t.id=f.task_id "
                "WHERE t.kind IN ('flash-preview','image-prepare') AND t.state='completed' ORDER BY t.updated DESC LIMIT 20").fetchall()
        for row in rows:
            old = json.loads(row[0]).get('preview_progress', {})
            if (old.get('executor') == executor and old.get('compressed') == compressed
                    and old.get('source_bytes', 0) > 0 and old.get('state') == 'completed'):
                histories.append(old)
        self.estimates = []
        for index, phase in enumerate(self.phases):
            samples = []
            for old in histories:
                step = next((step for step in old.get('stages', []) if step.get('phase') == phase
                             and step.get('state') == 'completed' and step.get('elapsed_seconds', 0) > .1), None)
                if step:
                    samples.append(step['elapsed_seconds'] * (self.size/old['source_bytes'] if phase in PHASES[:4] else 1))
            self.estimates.append(max(.2, statistics.median(samples) if samples else defaults[PHASES.index(phase)]))
        self.value = {'source_bytes': self.size, 'executor': executor, 'compressed': compressed,
                      'sample_count': len(histories), 'started': time.time(), 'state': 'running', 'active': True,
                      'stages': [{'phase': phase, 'state': 'pending', 'elapsed_seconds': 0,
                                  'estimated_seconds': self.estimates[index]} for index, phase in enumerate(self.phases)]}

    def event(self, event):
        phase = event.get('phase')
        if phase not in self.phases:
            return
        now = self.clock()
        index = self.phases.index(phase)
        if index != self.index:
            if self.index >= 0:
                old = self.value['stages'][self.index]
                old.update(state='completed', elapsed_seconds=round(now-self.phase_start, 3))
            for skipped in range(self.index+1, index):
                self.value['stages'][skipped]['state'] = 'skipped'
            self.index, self.phase_start = index, now
        step = self.value['stages'][index]
        done, total = event.get('done', 0), event.get('total', 0)
        elapsed = now-self.phase_start
        estimate = self.estimates[index]
        if elapsed >= 1 and total and done > 0:
            estimate = max(elapsed, elapsed*total/done)
        step.update(state='running', done=done, total=total, elapsed_seconds=round(elapsed, 3),
                    estimated_seconds=round(estimate, 3))
        self.value.update(phase=phase, done=done, total=total, elapsed_seconds=round(now-self.start, 3), updated=time.time())
        self._save()

    def finish(self, success):
        now = self.clock()
        if self.index >= 0:
            self.value['stages'][self.index].update(state='completed' if success else 'failed',
                elapsed_seconds=round(now-self.phase_start, 3))
        if success:
            for step in self.value['stages']:
                if step['state'] == 'pending':
                    step['state'] = 'skipped'
        self.value.update(state='completed' if success else 'failed', active=False,
                          elapsed_seconds=round(now-self.start, 3), updated=time.time())
        self._save()

    def _save(self):
        self.store.update_details(self.task_id, {'preview_progress': self.value})
