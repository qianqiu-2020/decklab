"""Prepare a board-scoped TitanX worker on a standalone Linux collector."""
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from .agent_grants import AgentGrantStore

CONFIG = Path('/etc/decklab/titanx.json')


GUI_CONFIG = Path('/var/lib/decklab-workbench/titanx.json')


def config_path():
    return CONFIG if CONFIG.exists() else GUI_CONFIG


def read_config(path=None):
    with (path or config_path()).open(encoding='utf-8') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('TitanX configuration is too large')
    value = json.loads(data)
    if not isinstance(value, dict) or value.keys() - {
            'broker', 'node_id', 'username', 'password', 'slots', 'first_com', 'api_url', 'api_token',
            'lab_id', 'project', 'projects', 'web_origin', 'accept_dispatch'}:
        raise ValueError('unknown TitanX configuration field')
    if path is not None:
        return value
    extra = board_additions(value)
    if extra:
        slots = dict(value.get('slots', extra['original_slots']))
        projects = dict(value.get('projects', {}))
        for key, board in extra['boards'].items():
            if key in slots and slots[key] != board['serial']:
                raise ValueError('TitanX board slot conflicts with the station configuration')
            slots[key] = board['serial']
            projects.setdefault(key, board['project'])
        if slots:
            assignments({'slots': slots}, {}, lambda: [])
            value.update(slots=slots, projects=projects)
        value.update(extra.get('connection', {}))
        value['paused_slots'] = extra.get('paused_slots', [])
    return value


def additions_path():
    return GUI_CONFIG.with_name('titanx-boards.json')


def station_key(settings):
    return [settings.get(key) for key in ('api_url', 'lab_id', 'broker', 'node_id')] + [settings.get('first_com', 1)]


def board_additions(settings):
    try:
        with additions_path().open() as stream:
            data = stream.read(65537)
    except FileNotFoundError:
        return {}
    if len(data) > 65536:
        raise ValueError('TitanX board configuration is too large')
    extra = json.loads(data)
    return extra if extra['station'] == station_key(settings) else {}


def write_private(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', delete=False) as target:
        name = target.name
        try:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


class BoardPending(Exception):
    pass


def assignments(settings, saved, discover):
    slots = settings.get('slots', saved.get('slots'))
    if slots is None:
        devices = [d for d in discover() if d.get('alive')]
        if not devices:
            raise BoardPending('waiting for a registered board')
        if len(devices) != 1:
            raise ValueError('multiple boards found; select TitanX boards in slots')
        if not devices[0].get('serial') or (devices[0].get('info') or {}).get('identity_status') != 'confirmed':
            raise BoardPending('waiting for confirmed board identity')
        slots = {'1': devices[0]['serial']}
    if not isinstance(slots, dict) or not slots:
        raise ValueError('slots must map positive slot numbers to board serials')
    result = {}
    for key, serial in slots.items():
        if (not re.fullmatch(r'[1-9][0-9]{0,4}', key) or int(key) > 65535
                or not isinstance(serial, str) or not serial.strip() or len(serial) > 128
                or not serial.isprintable() or serial in result.values()):
            raise ValueError('invalid or duplicate slot or board serial')
        result[int(key)] = serial
    return result


def connection_settings(settings):
    node = settings.get('node_id', '')
    broker = settings.get('broker', '')
    if not isinstance(node, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,79}', node):
        raise ValueError('set node_id to the unique TitanX station ID')
    url = urlsplit(broker)
    if (url.scheme not in ('tcp', 'ssl') or not url.hostname or not url.port or url.username is not None
            or url.path or url.query or url.fragment):
        raise ValueError('broker must contain an MQTT host and port; supply credentials separately')
    mqtt = {'broker': broker, 'client_id': node, 'topic_prefix': 'edge'}
    for key in ('username', 'password'):
        if key in settings:
            if not isinstance(settings[key], str):
                raise ValueError('invalid MQTT credential')
            mqtt[key] = settings[key]
    if mqtt.get('password') and not mqtt.get('username'):
        raise ValueError('MQTT password requires a username')
    return node, mqtt


def prepare(settings, state, origin, grants, discover):
    node, mqtt = connection_settings(settings)
    first = settings.get('first_com', 1)
    saved = json.loads(state.read_text()) if state.exists() else {}
    slots = assignments(settings, saved, discover)
    if type(first) is not int or first < 1 or first + max(slots) - 1 > 65535:
        raise ValueError('RemoteUART COM outside 1..65535')
    paused = set(settings.get('paused_slots', []))
    slots = {i: serial for i, serial in slots.items() if str(i) not in paused}
    tokens = dict(saved.get('tokens', {}))
    for serial in slots.values():
        if serial in tokens:
            grant = grants.lookup(tokens[serial])
            if not grant or grant['scope'] != 'device' or grant['serial'] != serial:
                raise ValueError('stored TitanX grant revoked or mismatched; review the station authorization')
        else:
            tokens[serial], grant = grants.issue('device', serial)
            try:
                write_private(state, json.dumps({'slots': slots, 'tokens': tokens}))
            except OSError:
                grants.revoke_request(grant['id'])
                raise
    for removed in tokens.keys() - set(slots.values()):
        grant = grants.lookup(tokens.pop(removed))
        if grant:
            grants.revoke_request(grant['id'])
    write_private(state, json.dumps({'slots': slots, 'tokens': tokens}))
    if not slots:
        return ''
    # YAML integer keys are required by Edge. Quote every free-form string.
    lines = ['backend: decklab', 'node: ' + json.dumps({'id': node, 'name': node}), 'mqtt: ' + json.dumps(mqtt),
             'dts: {topic_prefix: dts}', 'slots: ' + json.dumps([{'id': i} for i in sorted(slots)]),
             'decklab:', '  base_url: ' + json.dumps(origin)]
    for key, values in [('slots', slots), ('slot_tokens', {i: tokens[s] for i, s in slots.items()}),
                        ('remote_uart_com', {i: first + i - 1 for i in slots})]:
        lines.append('  ' + key + ':')
        lines.extend('    %d: %s' % (i, json.dumps(values[i])) for i in sorted(values))
    return '\n'.join(lines) + '\n'


def main():
    from .config import BASE, SOCKET, SETTINGS
    from .control_server import UnixHTTPConnection

    def discover():
        conn = UnixHTTPConnection(SOCKET, timeout=5)
        try:
            conn.request('GET', '/api/devices')
            response = conn.getresponse()
            if response.status != 200:
                raise ConnectionError('collector unavailable')
            return json.loads(response.read())['devices']
        finally:
            conn.close()

    environment = SETTINGS.effective()['environment']
    host = environment['BIND']
    if host not in ('127.0.0.1', '::1', 'localhost', '0.0.0.0', '::'):
        raise ValueError('TitanX worker requires a local DeckLab listener')
    host = '[::1]' if ':' in host else '127.0.0.1'
    origin = 'http://%s:%s' % (host, environment['PORT'])
    state = Path(BASE) / 'titanx-worker.json'
    grants = AgentGrantStore(str(Path(BASE) / 'agent-grants.sqlite3'))
    try:
        while True:
            try:
                content = prepare(read_config(), state, origin, grants, discover)
                break
            except (BoardPending, ConnectionError, FileNotFoundError) as error:
                if not config_path().exists():
                    raise
                print('Waiting for DeckLab collector or board:', type(error).__name__, flush=True)
                time.sleep(5)
        write_private(Path('/run/decklab-titanx-config/edge.yaml'), content)
    finally:
        grants.close()


if __name__ == '__main__':
    main()
