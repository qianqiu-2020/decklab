"""Print browser access links from the existing local Web service.

This is an installer/administrator entry point, not a second service or a node
pairing protocol. Share links use the existing API and browser session cookie.
"""
import argparse
import http.client
import ipaddress
import json
from pathlib import Path
import shlex
from urllib.request import ProxyHandler, Request, build_opener

from .web_settings import WebSettings


def read_environment(path):
    raw = Path(path).read_text(encoding='utf-8-sig')
    if len(raw.encode()) > 65536:
        raise ValueError('configuration exceeds size limit')
    values = {}
    for line in raw.splitlines():
        words = shlex.split(line, comments=True)
        if len(words) == 1 and words[0].startswith('SERIALD_') and '=' in words[0]:
            key, value = words[0].split('=', 1)
            values[key] = value
    return values


def links(environment, opener=None):
    base = environment.get('SERIALD_BASE', '/data/seriald-data')
    effective = WebSettings(Path(base) / 'web-settings.json', environment).effective()['environment']
    bind, port, token = effective['BIND'], int(effective['PORT']), effective['AUTH_TOKEN']
    host = '127.0.0.1' if bind in ('0.0.0.0', '::', 'localhost') else bind
    authority = ('[' + host + ']' if ':' in host else host) + ':' + str(port)
    opener = opener or build_opener(ProxyHandler({}))
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    def request(path, post=False):
        with opener.open(Request('http://' + authority + path, data=b'' if post else None, headers=headers), timeout=2) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('local access response exceeds size limit')
        return json.loads(raw)
    access = request('/api/access')
    path, expires = '/', None
    if not access.get('open_access'):
        share = request('/api/share-link', True)
        path, expires = share.get('path'), share.get('expires_in')
        if not isinstance(path, str) or not path.startswith('/share/') or any(c in path for c in '\r\n?#'):
            raise ValueError('invalid local share response')
    hosts = [host]
    if bind in ('0.0.0.0', '::'):
        for item in access.get('lan_hosts', []):
            try:
                address = ipaddress.ip_address(item)
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
                hosts.append(str(address))
    urls = ['http://' + ('[' + h + ']' if ':' in h else h) + ':' + str(port) + path for h in dict.fromkeys(hosts)]
    return {'urls': urls, 'expires_in': expires, 'browser_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/seriald/seriald.env'))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = links(read_environment(args.config))
    except (OSError, ValueError, http.client.HTTPException):
        parser.exit(1, 'DeckLab 尚未就绪或凭证不可读取；服务启动后重新运行 seriald-access。\n')
    if args.json:
        print(json.dumps(result))
    else:
        print('在其他电脑的浏览器打开下列局域网链接，无需安装客户端或配对：')
        for url in result['urls']:
            print('  ' + url)
        if result['expires_in']:
            print('访问授权有效期：%s 小时；需要新链接时运行 sudo seriald-access。' % (result['expires_in'] / 3600))


if __name__ == '__main__':
    main()
