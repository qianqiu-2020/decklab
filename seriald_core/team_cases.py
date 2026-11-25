"""Read the test platform's catalogue with a controller-owned credential."""
import http.client
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlsplit

from .tasks import TaskError


class TeamCases:
    def __init__(self, origin, token):
        try:
            address = urlsplit(origin)
            port = address.port
        except ValueError as error:
            raise TaskError('测试平台地址无效', 503) from error
        if (address.scheme != 'https' or not address.hostname or address.username is not None
                or address.query or address.fragment or address.path.rstrip('/')):
            raise TaskError('测试平台地址须为 HTTPS 站点', 503)
        if not token or len(token) > 4096 or not token.isascii() or not token.isprintable():
            raise TaskError('测试平台令牌无效', 503)
        self.host, self.port, self.token = address.hostname, port, token

    @classmethod
    def configured(cls):
        from .config import SETTINGS
        saved = SETTINGS.read().get('environment', {})
        if 'TEAM_CATALOGUE_URL' in saved or 'TEAM_CATALOGUE_TOKEN' in saved:
            origin, token = saved.get('TEAM_CATALOGUE_URL'), saved.get('TEAM_CATALOGUE_TOKEN')
            return cls(origin, token) if origin or token else None
        origin = os.environ.get('SERIALD_TEST_PLATFORM_URL', '')
        path = os.environ.get('SERIALD_TEST_PLATFORM_TOKEN_FILE', '')
        if not origin and not path:
            return None
        try:
            with Path(path).open(encoding='ascii') as source:
                token = source.read(4097)
            if len(token) > 4096:
                raise ValueError
        except (ValueError, OSError) as error:
            raise TaskError('无法读取测试平台连接配置', 503) from error
        return cls(origin, token.strip())

    def request(self, path, body=None):
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=10,
                                           context=ssl.create_default_context())
        try:
            data = None if body is None else json.dumps(body).encode()
            conn.request('GET' if body is None else 'POST', '/api' + path, body=data,
                         headers={'Authorization': self.token, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            if response.status != 200:
                raise TaskError(f'测试平台返回 HTTP {response.status}', 502)
            payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError
            obj = json.loads(payload)
            if not isinstance(obj, dict) or obj.get('code') != 20000:
                raise TaskError('测试平台未接受请求，请检查平台权限', 502)
            return obj['data']
        except TaskError:
            raise
        except (OSError, http.client.HTTPException, UnicodeError, KeyError, ValueError) as error:
            raise TaskError('读取测试平台响应失败', 502) from error
        finally:
            conn.close()

    def catalogue(self, query):
        page = query.get('page', ['1'])[0]
        name = query.get('name', [''])[0].strip()
        if not page.isascii() or not page.isdecimal() or len(page) > 6 or int(page) < 1 or len(name) > 200:
            raise TaskError('用例查询参数无效')
        data = self.request('/caseInfo/page', {'pageNo': int(page), 'pageSize': 20, 'caseName': name})
        try:
            if not isinstance(data['list'], list) or type(data['total']) is not int:
                raise ValueError
            cases = [{'id': item['id'], 'name': item['caseName'], 'code': item['caseCode'],
                      'automated': item.get('isAutomated') is True and item.get('isAutomationCompleted') is True,
                      'reviewed': item.get('reviewStatus') == 2} for item in data['list']]
            return {'configured': True, 'page': int(page), 'page_size': 20, 'total': data['total'], 'cases': cases}
        except (TypeError, KeyError, ValueError) as error:
            raise TaskError('测试平台返回了无效的用例目录', 502) from error

    @staticmethod
    def number(value):
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) <= 0:
            raise TaskError('团队用例编号无效')
        return int(value)

    def browse(self, query):
        view = query.get('view', ['cases'])[0]
        if view == 'cases':
            return self.catalogue(query)
        try:
            if view == 'groups':
                rows = self.request('/caseGroup/list')
                return {'configured': True, 'groups': [{'id': r['id'], 'name': r['groupName']} for r in rows]}
            key = self.number(query.get('id', [''])[0])
            if view == 'members':
                rows = self.request(f'/caseGroup/case/{key}', {})
                if any(r['caseGroupId'] != key for r in rows):
                    raise ValueError
                return {'configured': True, 'members': [
                    {'id': r['caseId'], 'name': r['caseName'], 'code': r['caseCode'],
                     'order': r['executeOrder'], 'times': r['times']} for r in rows]}
            if view != 'detail':
                raise TaskError('未知团队用例查询')
            case = self.request(f'/caseInfo/{key}')
            steps = self.request(f'/caseInfo/caseStep?caseId={key}')
            if case['id'] != key or any(s['caseId'] != key for s in steps):
                raise ValueError
            return {'configured': True, 'case': {
                'id': key, 'name': case['caseName'], 'code': case['caseCode'],
                'automated': case.get('isAutomated') is True and case.get('isAutomationCompleted') is True,
                'reviewed': case.get('reviewStatus') == 2, 'prerequisite': case.get('testPreCondition') or '',
                'steps': [{'command': s['stepInfo'], 'expected': s['expectedResult']} for s in steps]}}
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise TaskError('测试平台返回了无效的用例定义', 502) from error
