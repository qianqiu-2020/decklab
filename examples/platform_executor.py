#!/usr/bin/env python3
"""Small HTTP integration example; no collectors, MCP runtime or board discovery loop."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serialctl import Client, ClientError, TaskOutcome, TaskReceipt


TERMINAL = ('completed', 'failed', 'cancelled', 'interrupted')


def run_attempt(client, mapping, kind, request_id, wait_seconds=30, lease_ttl=300):
    """The caller's test job owns request_id and the platform-to-board mapping."""
    client.negotiate(kind)
    client.select_mapping(mapping)
    claim = client.claim_board(lease_ttl)
    # Do not put release in a finally block: a lost response can leave execution
    # running. Keeping a recovery hold is intentional until the original is checked.
    result = client.submit_task(kind, request_id)
    deadline, renew_at = time.monotonic() + wait_seconds, time.monotonic() + lease_ttl / 3
    while True:
        receipt = TaskReceipt.from_response(result, request_id)
        if receipt.state in TERMINAL:
            break
        if time.monotonic() >= deadline:
            return {'platform_device_id': mapping['platform_device_id'], 'task': result['task'],
                    'input_owner': claim['input_owner'], 'wait_expired': True,
                    'lease_released': False, 'next_action': 'query-original-task'}
        if time.monotonic() >= renew_at:
            claim = client.renew_board(lease_ttl)
            renew_at = time.monotonic() + lease_ttl / 3
        time.sleep(0.1)
        result = client.task_by_request(request_id)
    released = False
    if receipt.outcome not in (TaskOutcome.UNKNOWN, TaskOutcome.ISSUED):
        client.release_board()
        released = True
    return {'platform_device_id': mapping['platform_device_id'], 'task': result['task'],
            'lease_released': released,
            'next_action': 'evaluate-test-evidence' if released else 'observe-original-operation'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('SERIALD_URL', 'http://127.0.0.1:8898'))
    parser.add_argument('--mapping', help='verified platform-device.json; contains no credentials')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('inspect', help='read capabilities, mapped identity and public ownership')
    run = commands.add_parser('run', help='execute one already-authorized platform attempt')
    run.add_argument('--kind', required=True, choices=('identify', 'hardware-reset', 'hardware-power-on',
                                                      'hardware-power-off', 'hardware-enter-download'))
    run.add_argument('--request-id', required=True, help='saved by the original platform job before submission')
    run.add_argument('--wait', type=float, default=30)
    recover = commands.add_parser('query', help='read an original request only; never claim or submit')
    recover.add_argument('--request-id', required=True)
    revoke = commands.add_parser('revoke', help='owner revokes a grant; no board command or lease release')
    revoke.add_argument('--grant-id', required=True)
    args = parser.parse_args(argv)
    client = Client(args.url, timeout=10, token=os.environ.get('SERIALD_TOKEN', ''))
    try:
        if args.action == 'query':
            client.negotiate()
            result = client.task_by_request(args.request_id)
        elif args.action == 'revoke':
            # This command needs the primary credential, supplied separately from
            # the platform executor's grant. It never automatically releases a lease.
            client.token = os.environ.get('SERIALD_OWNER_TOKEN', '')
            if not client.token:
                raise ClientError('Set SERIALD_OWNER_TOKEN for this explicit owner operation')
            result = client.revoke_grant(args.grant_id)
        else:
            if not args.mapping:
                raise ClientError('--mapping is required for inspect/run')
            mapping = json.loads(Path(args.mapping).read_text(encoding='utf-8'))
            if args.action == 'inspect':
                result = {'capabilities': client.negotiate(), 'device': client.select_mapping(mapping),
                          'ownership': client.input_owner()}
            else:
                if not 0 < args.wait <= 3600:
                    raise ClientError('--wait must be greater than zero and at most 3600 seconds')
                result = run_attempt(client, mapping, args.kind, args.request_id, args.wait)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ClientError, OSError, ValueError) as error:
        result = {'ok': False, 'error': str(error), 'lease_released': False}
        for name in ('code', 'status', 'request_id', 'task_id', 'outcome'):
            value = getattr(error, name, None)
            if value is not None:
                result[name] = value
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
