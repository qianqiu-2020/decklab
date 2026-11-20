"""Run one administrator-bound Home Assistant board action for DeckLab."""
import argparse
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def call(origin, token_file, entity, check=False):
    url = urlsplit(origin)
    if (url.scheme not in ('http', 'https') or not url.hostname or url.username is not None
            or url.password is not None or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('expected the Home Assistant origin')
    if not re.fullmatch(r'(switch|button)\.[a-z0-9_]+', entity):
        raise ValueError('expected one switch or button entity')
    token = Path(token_file).read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9._~-]+', token):
        raise ValueError('invalid token file')
    connection = (HTTPSConnection if url.scheme == 'https' else HTTPConnection)(
        url.hostname, url.port, timeout=15)
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    try:
        if check:
            connection.request('GET', '/api/states/' + entity, headers=headers)
        else:
            service = 'switch/turn_on' if entity.startswith('switch.') else 'button/press'
            connection.request('POST', '/api/services/' + service,
                               body=json.dumps({'entity_id': entity}), headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('Home Assistant returned HTTP %d' % response.status)
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Home Assistant response too large')
        data = json.loads(raw)
        if check:
            # An unused HA button may have state "unknown" until its first press.
            if not isinstance(data, dict) or data.get('entity_id') != entity or data.get('state') == 'unavailable':
                raise ValueError('configured entity is missing or unavailable')
            return data['state']
        if not isinstance(data, list):
            raise ValueError('unexpected Home Assistant service receipt')
        return 'acknowledged'
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read the entity state without activating it')
    parser.add_argument('origin')
    parser.add_argument('token_file')
    parser.add_argument('entity')
    args = parser.parse_args()
    try:
        print(call(args.origin, args.token_file, args.entity, args.check))
    except (OSError, HTTPException, ValueError, KeyError) as error:
        # Never emit a response body, token, or request headers.
        sys.exit(str(error) if type(error) is ValueError else 'Home Assistant request failed')
