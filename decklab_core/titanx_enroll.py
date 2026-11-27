"""Register configured workbench slots through the platform's existing admin API."""
import json
from pathlib import Path
import time

from .tasks import TaskError
from .team_cases import TeamCases
from .titanx_workbench import read_config, write_private


def enroll(settings, slots, platform, journal):
    lab, node = settings['lab_id'], settings['node_id']
    project, origin = settings.get('project'), settings.get('web_origin')
    if not isinstance(origin, str) or not origin:
        raise ValueError('set web_origin for station enrollment')
    if type(lab) is not int or lab < 1 or not isinstance(project, str) or not project.strip():
        raise ValueError('set lab_id and project for station enrollment')
    # Validate the browser origin using the same HTTPS origin constraints as the platform client.
    TeamCases(origin, 'validate-origin')
    dispatch = settings.get('accept_dispatch', False)
    if type(dispatch) is not bool:
        raise ValueError('accept_dispatch must be a boolean')
    pending = json.loads(journal.read_text()) if journal.exists() else None

    def create_once(path, body):
        record = {'site': settings['api_url'], 'path': path, 'record': body}
        if pending == record:
            return
        if pending is not None:
            raise ValueError('previous registration is unconfirmed; check the original record before retrying')
        write_private(journal, json.dumps(record))
        platform.request(path, body)

    def confirmed(path, body):
        nonlocal pending
        if pending == {'site': settings['api_url'], 'path': path, 'record': body}:
            journal.unlink()
            pending = None

    nodes = [n for n in platform.request('/deviceNode/list', {'namespaceId': lab})
             if n['originNodeId'] == node]
    node_record = {'namespaceId': lab, 'originNodeId': node, 'name': node, 'decklabWebOrigin': origin}
    if not nodes:
        create_once('/deviceNode/add', node_record)
        return False
    if len(nodes) != 1 or nodes[0]['namespaceId'] != lab or nodes[0].get('decklabWebOrigin') != origin:
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
    for key, serial in slots.items():
        slot = int(key)
        com = settings.get('first_com', 1) + slot - 1
        code = '%s:%s' % (node, slot)
        body = {'nodeId': node_id, 'code': code, 'name': code, 'project': project,
                'decklabSlot': slot, 'decklabSerial': serial, 'remoteComserver': 100, 'remoteCom': com,
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
            if len(candidates) != 1 or any(candidates[0].get(k) != body[k] for k in
                                          ('nodeId', 'decklabSlot', 'decklabSerial', 'remoteComserver', 'remoteCom')):
                raise ValueError('existing board archive conflicts with the configured board or COM')
            confirmed('/deviceBoard/add', body)
            continue
        live = [r for r in reports if r.get('nodeId') == node and r.get('deviceId') == code]
        if not live:
            complete = False
            continue
        if len(live) != 1 or any(live[0].get(k) != body[k] for k in
                                ('decklabSlot', 'decklabSerial', 'remoteComserver', 'remoteCom')):
            raise ValueError('TitanX reported a different physical board or RemoteUART port')
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
