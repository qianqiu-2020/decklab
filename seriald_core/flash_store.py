"""Flash metadata in the existing task registry, never a second execution queue."""
import hashlib
import json
import re
import secrets
import time

from .tasks import TaskError, _check_result_fields


MAX_EVENTS = 256


def encode_document(document, limit=65536):
    if not isinstance(document, dict):
        raise TaskError('flash metadata must be an object')
    _check_result_fields(document)
    try:
        encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError) as error:
        raise TaskError('invalid flash metadata') from error
    if len(encoded.encode()) > limit:
        raise TaskError('flash metadata too large')
    return encoded


def validate_request(request_id, input_digest):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):
        raise TaskError('invalid flash request ID')
    if not isinstance(input_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', input_digest):
        raise TaskError('invalid flash input digest')


class FlashStore:
    def __init__(self, store):
        self.store = store

    def put_plan(self, document):
        """Persist a bounded, credential-free immutable plan and content digest."""
        encoded = encode_document(document, 1024 * 1024)
        actor, serial, target_id = document.get('actor'), document.get('serial'), document.get('target_id')
        if (not isinstance(actor, str) or not actor or len(actor) > 80
                or not isinstance(serial, str) or len(serial.encode()) > 100 or not (serial or target_id)):
            raise TaskError('flash plan requires actor and a board or USB target')
        if target_id is not None and (not isinstance(target_id, str) or not target_id or len(target_id) > 80):
            raise TaskError('invalid flash plan target')
        if target_id and actor != 'web':
            raise TaskError('automatic flash targets require a human operator', 403)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        plan_id = secrets.token_hex(12)
        with self.store.lock, self.store.db:
            if serial and self.store.db.execute('SELECT serial FROM boards WHERE serial=?', (serial,)).fetchone() is None:
                raise TaskError('flash board is not registered', 404)
            if target_id:
                target = self.store.db.execute('SELECT generation,serial,present,document FROM flash_targets WHERE id=?',
                                               (target_id,)).fetchone()
                current = (target is not None and target['present'] and (target['serial'] or '') == serial
                           and document.get('target_generation') == target['generation'])
                if not current:
                    task_id = document.get('selection_task_id')
                    task = self.store.db.execute('SELECT kind,actor,state FROM tasks WHERE id=?',
                                                 (task_id,)).fetchone() if isinstance(task_id, str) else None
                    snapshot = self.store.db.execute('SELECT generation FROM task_flash_targets '
                        'WHERE task_id=? AND target_id=?', (task_id, target_id)).fetchone() if task else None
                    hold = self.store.db.execute('SELECT task_id,state FROM flash_resource_holds WHERE resource=?',
                        ('flash-target:' + target_id,)).fetchone() if task else None
                    detail_row = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?',
                                                       (task_id,)).fetchone() if task else None
                    details = json.loads(detail_row['document']) if detail_row else {}
                    boards = [row[0] for row in self.store.db.execute(
                        'SELECT serial FROM task_board_targets WHERE task_id=?', (task_id,))] if task else []
                    permitted = (target is not None and target['present'] and task is not None
                        and task['kind'] == 'flash' and task['actor'] == actor and task['state'] == 'running'
                        and snapshot is not None and snapshot['generation'] == document.get('target_generation')
                        and hold is not None and hold['task_id'] == task_id and hold['state'] == 'active'
                        and boards == ([serial] if serial else [])
                        and target['generation'] == details.get('allowed_final_generation')
                        and details.get('execution_device') is not None
                        and json.loads(target['document']).get('device') == details['execution_device']
                        and document.get('execution_device') == details['execution_device'])
                    if not permitted:
                        raise TaskError('flash USB target generation or identity changed', 409)
                if target is None:
                    raise TaskError('flash USB target generation or identity changed', 409)
            self.store.db.execute('INSERT INTO flash_plans VALUES(?,?,?,?,?,?,?)',
                                  (plan_id, actor, serial or None, digest, encoded, time.time(), target_id))
        return dict(document, plan_id=plan_id, digest=digest)

    def get_plan(self, plan_id, actor='web', serial=None, target_id=None):
        if not isinstance(plan_id, str) or not re.fullmatch(r'[0-9a-f]{24}', plan_id):
            raise TaskError('flash plan not found', 404)
        with self.store.lock:
            row = self.store.db.execute('SELECT * FROM flash_plans WHERE id=?', (plan_id,)).fetchone()
        if (row is None or actor != 'web' and row['actor'] != actor
                or serial is not None and (row['serial'] or '') != serial
                or target_id is not None and row['target_id'] != target_id):
            raise TaskError('flash plan not found', 404)
        return dict(json.loads(row['document']), plan_id=row['id'], digest=row['digest'])

    def find_request(self, actor, request_id, input_digest):
        validate_request(request_id, input_digest)
        with self.store.lock:
            row = self.store.db.execute('SELECT input_digest,task_id FROM flash_requests '
                                        'WHERE actor=? AND request_id=?', (actor, request_id)).fetchone()
        if row is None:
            return None
        if row['input_digest'] != input_digest:
            raise TaskError('flash request ID was already used with different input', 409)
        return row['task_id']

    def details(self, task_id):
        with self.store.lock:
            row = self.store.db.execute('SELECT document FROM flash_details WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise TaskError('flash task not found', 404)
        return json.loads(row['document'])

    def public_details(self, task_id):
        """Expose domain results while keeping recovery/cache paths server-side."""
        from .flash_runner import public_plan
        return public_plan(self.details(task_id))

    def update_details(self, task_id, values):
        encode_document(values)
        with self.store.lock, self.store.db:
            document = self.details(task_id)
            document.update(values)
            encoded = encode_document(document)
            self.store.db.execute('UPDATE flash_details SET document=? WHERE task_id=?', (encoded, task_id))
        return document

    def append_event(self, task_id, event):
        encoded = encode_document(event, 8192)
        with self.store.lock, self.store.db:
            row = self.store.db.execute('SELECT next_event FROM flash_details WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise TaskError('flash task not found', 404)
            cursor = row[0] + 1
            self.store.db.execute('UPDATE flash_details SET next_event=? WHERE task_id=?', (cursor, task_id))
            self.store.db.execute('INSERT INTO flash_events VALUES(?,?,?)', (task_id, cursor, encoded))
            self.store.db.execute('DELETE FROM flash_events WHERE task_id=? AND cursor<=?',
                                  (task_id, cursor - MAX_EVENTS))
        return cursor

    def events(self, task_id, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= MAX_EVENTS:
            raise TaskError('invalid flash event cursor')
        with self.store.lock:
            self.details(task_id)
            first = self.store.db.execute('SELECT MIN(cursor) FROM flash_events WHERE task_id=?', (task_id,)).fetchone()[0]
            rows = self.store.db.execute('SELECT cursor,document FROM flash_events WHERE task_id=? AND cursor>? '
                                         'ORDER BY cursor LIMIT ?', (task_id, after, limit)).fetchall()
        return {'events': [dict(json.loads(row['document']), cursor=row['cursor']) for row in rows],
                'next_cursor': rows[-1]['cursor'] if rows else after,
                'gap': first is not None and after < first - 1}
