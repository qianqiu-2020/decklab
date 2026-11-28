"""Register configured workbench slots through the platform's existing admin API."""
import json
from pathlib import Path
import time
from urllib.parse import urlencode

from .tasks import TaskError
from .team_cases import TeamCases
from .titanx_workbench import read_config, write_private


def archive_matches(board, node_id, device_id, serial, slot, com):
    code = board.get('code')
    return (isinstance(code, str) and bool(code) and board.get('nodeId') == node_id
            and (board.get('remoteComserver'), board.get('remoteCom')) == (100, com)
            and board.get('deviceId') in (None, device_id)
            and board.get('decklabSlot') in (None, slot)
            and board.get('decklabSerial') in (None, serial)
            and (board.get('deviceId') == device_id or board.get('code') == device_id
                 or board.get('decklabSerial') == serial
                 or (board.get('deviceId') is None and not code.startswith(device_id.rsplit(':', 1)[0] + ':'))))


def local_usb(serial):
    from .config import SOCKET
    from .control_server import UnixHTTPConnection

    def get(path):
        connection = UnixHTTPConnection(SOCKET, timeout=10)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            if response.status in (404, 409, 503):
                return {}
            if response.status != 200:
                raise ConnectionError('collector query failed')
            return json.loads(response.read())
        finally:
            connection.close()

    rows = [r for r in get('/api/devices').get('devices', []) if r.get('serial') == serial
            and r.get('alive') and (r.get('info') or {}).get('identity_status') == 'confirmed']
    if len(rows) != 1:
        return None
    board = rows[0]
    query = {k: board[k] for k in ('key', 'serial', 'connection_id')}
    query['adb_binding'] = 1
    binding = get('/api/hardware' + '?' + urlencode(query)).get('adb_binding', {})
    if (binding.get('serial') == serial and binding.get('connection_id') == board['connection_id']
            and binding.get('usb_port')):
        return 'usb:' + binding['usb_port']
    return None


def enroll(settings, slots, platform, journal, usb_for=local_usb):
    lab, node = settings['lab_id'], settings['node_id']
    project, origin = settings.get('project'), settings.get('web_origin')
    if type(lab) is not int or lab < 1 or not isinstance(project, str) or not project.strip():
        raise ValueError('set lab_id and project for station enrollment')
    # Validate the browser origin using the same HTTPS origin constraints as the platform client.
    if origin:
        TeamCases(origin, 'validate-origin')
    dispatch = settings.get('accept_dispatch', False)
    if type(dispatch) is not bool:
        raise ValueError('accept_dispatch must be a boolean')
    pending = json.loads(journal.read_text()) if journal.exists() else None

    def pending_matches(path, body):
        if not pending or pending.get('site') != settings['api_url'] or pending.get('path') != path:
            return False
        old = dict(pending.get('record', {}))
        if 'decklabWebOrigin' in old and old.pop('decklabWebOrigin') != origin:
            return False
        if 'decklabSlot' in old or 'decklabSerial' in old:
            key = str(old.pop('decklabSlot', None))
            if (old.pop('decklabSerial', None) != slots.get(key)
                    or old.get('code') != node + ':' + key):
                return False
        return old == body

    def create_once(path, body):
        record = {'site': settings['api_url'], 'path': path, 'record': body}
        if pending_matches(path, body):
            return
        if pending is not None:
            raise ValueError('previous registration is unconfirmed; check the original record before retrying')
        write_private(journal, json.dumps(record))
        platform.request(path, body)

    def confirmed(path, body):
        nonlocal pending
        if pending_matches(path, body):
            journal.unlink()
            pending = None

    nodes = [n for n in platform.request('/deviceNode/list', {'namespaceId': lab})
             if n['originNodeId'] == node]
    node_record = {'namespaceId': lab, 'originNodeId': node, 'name': node}
    if not nodes:
        create_once('/deviceNode/add', node_record)
        return False
    if len(nodes) != 1 or nodes[0]['namespaceId'] != lab or (origin and nodes[0].get('decklabWebOrigin') not in (None, origin)):
        raise ValueError('existing TitanX node has a different lab or DeckLab browser origin')
    confirmed('/deviceNode/add', node_record)
    node_id = nodes[0]['id']
    registered = platform.request('/deviceNode/listVO', {'namespaceId': lab})
    matching = [n for n in registered if n['id'] == node_id and n['originNodeId'] == node]
    if len(matching) != 1:
        raise ValueError('cannot verify the TitanX node archive')
    boards = matching[0].get('deviceBoards') or []
    reports = platform.request('/deviceBoard/unregistered', {'namespaceId': lab})
    complete = True
    # Reconcile a pending POST before a newly online sibling can submit another.
    pending_com = (pending or {}).get('record', {}).get('remoteCom')
    for key, serial in sorted(slots.items(), key=lambda item: settings.get('first_com', 1) + int(item[0]) - 1 != pending_com):
        slot = int(key)
        com = settings.get('first_com', 1) + slot - 1
        code = '%s:%s' % (node, slot)
        body = {'nodeId': node_id, 'code': code, 'name': code,
                'project': settings.get('projects', {}).get(str(slot), project),
                'remoteComserver': 100, 'remoteCom': com,
                'noDispatch': not dispatch, 'autoRecoverDisabled': True}
        for other in registered:
            if other['id'] == node_id:
                continue
            if any((b.get('remoteComserver'), b.get('remoteCom')) == (100, com)
                   or b.get('decklabSerial') == serial for b in other.get('deviceBoards') or []):
                raise ValueError('another TitanX node already uses this board or COM')
        candidates = [b for b in boards if b.get('code') == code or b.get('decklabSlot') == slot
                      or b.get('decklabSerial') == serial
                      or (b.get('remoteComserver'), b.get('remoteCom')) == (100, com)]
        if candidates:
            if len(candidates) != 1 or not archive_matches(candidates[0], node_id, code, serial, slot, com):
                raise ValueError('existing board archive conflicts with the configured board or COM')
            confirmed('/deviceBoard/add', body)
            continue
        live = [r for r in reports if r.get('nodeId') == node and r.get('deviceId') == code]
        if not live:
            if pending_matches('/deviceBoard/add', body):
                return False
            complete = False
            continue
        report = live[0]
        if (len(live) != 1 or (report.get('remoteComserver'), report.get('remoteCom')) != (100, com)
                or report.get('decklabSlot') not in (None, slot)
                or report.get('decklabSerial') not in (None, serial)):
            raise ValueError('TitanX reported a different physical board or RemoteUART port')
        if report.get('decklabSerial') is None:
            usb = usb_for(serial)
            if not usb:
                complete = False
                continue
            if report.get('serial') != usb:
                raise ValueError('TitanX USB path differs from the confirmed DeckLab board')
        create_once('/deviceBoard/add', body)
        return False
    if pending is not None:
        raise ValueError('unconfirmed registration no longer matches this station configuration')
    return complete


def main():
    from .config import BASE
    settings = read_config()
    if 'lab_id' not in settings:
        return
    platform = TeamCases(settings['api_url'], settings['api_token'])
    state = Path(BASE) / 'titanx-worker.json'
    slots = json.loads(state.read_text())['slots']
    if not slots:
        return
    journal = Path(BASE) / 'titanx-enroll-pending.json'
    while True:
        try:
            if enroll(settings, slots, platform, journal):
                break
        except (TaskError, OSError) as error:
            print('TitanX registration awaiting readback:', error, flush=True)
            time.sleep(25)
        time.sleep(5)
    print('TitanX station and board archives verified', flush=True)


if __name__ == '__main__':
    main()
