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
import time
from urllib.error import HTTPError
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/etc/seriald/seriald.env'))
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--wait', type=float, default=0, metavar='SECONDS',
                        help='wait up to 60 seconds for the installed Web service')
    args = parser.parse_args(argv)
    if not 0 <= args.wait <= 60:
        parser.error('--wait must be between 0 and 60 seconds')
    try:
        environment = read_environment(args.config)
    except (OSError, ValueError):
        parser.exit(1, '无法读取 DeckLab 配置；请使用 sudo seriald-access。\n')
    deadline = time.monotonic() + args.wait
    while True:
        try:
            result = links(environment)
            break
        except HTTPError as error:
            if error.code in (401, 403):
                parser.exit(1, 'DeckLab 访问凭证与运行服务不一致；请核对服务配置。\n')
            if error.code < 500:
                parser.exit(1, 'DeckLab 访问地址请求失败；请核对服务配置。\n')
        except ValueError:
            parser.exit(1, 'DeckLab 访问配置或响应无效；请核对服务配置。\n')
        except (OSError, http.client.HTTPException):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            parser.exit(1, 'DeckLab 服务未就绪；请查看 systemctl status seriald 和 journalctl -u seriald。\n')
        time.sleep(min(.25, remaining))
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
