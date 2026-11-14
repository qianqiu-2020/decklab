import io
import json
from pathlib import Path
import tempfile
import unittest
from seriald_core.access_info import links, read_environment


class AccessTests(unittest.TestCase):
    def test_installed_environment_is_parsed_without_shell_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'service.env'
            file.write_text('# comment\nSERIALD_PORT=1234\nSERIALD_AUTH_TOKEN="has space"\nexport WRONG=x\nSERIALD_VALUE=\'$(no-command)\'\n')
            env = read_environment(file)
            self.assertEqual(env['SERIALD_AUTH_TOKEN'], 'has space')
            self.assertEqual(env['SERIALD_VALUE'], '$(no-command)')
            self.assertNotIn('WRONG',env)

    def test_password_uses_existing_share_api_never_prints_owner_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            requests=[]
            class Opener:
                def open(self, request, timeout):
                    requests.append(request)
                    return io.BytesIO(json.dumps({'open_access':False,'lan_hosts':['10.0.0.3','::1','evil.test']} if request.full_url.endswith('/api/access') else {'path':'/share/signed','expires_in':86400}).encode())
            result = links({'SERIALD_BASE':directory,'SERIALD_BIND':'0.0.0.0','SERIALD_PORT':'1234','SERIALD_AUTH_TOKEN':'owner-secret'},Opener())
            self.assertEqual(result['urls'],['http://127.0.0.1:1234/share/signed','http://10.0.0.3:1234/share/signed'])
            self.assertNotIn('owner-secret',json.dumps(result))
            self.assertEqual(requests[1].get_method(),'POST')
            self.assertEqual(requests[0].headers['Authorization'],'Bearer owner-secret')

    def test_saved_configuration_and_ipv6_are_used(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'web-settings.json').write_text(json.dumps({'version':1,'environment':{'BIND':'::1','PORT':'1235'},'documents':{}}))
            class Opener:
                def open(self, request, timeout):
                    self.url=request.full_url
                    return io.BytesIO(b'{"open_access":true}')
            opener=Opener();result=links({'SERIALD_BASE':directory},opener)
            self.assertEqual(opener.url,'http://[::1]:1235/api/access')
            self.assertEqual(result['urls'],['http://[::1]:1235/'])

    def test_real_service_share_link_grants_browser_cookie_without_owner_password(self):
        from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler
        from http.cookiejar import CookieJar
        from urllib.parse import urlsplit
        from test_connection_access import protected_connection
        with protected_connection() as fixture, tempfile.TemporaryDirectory() as directory:
            port=str(urlsplit(fixture.url).port)
            result=links({'SERIALD_BASE':directory,'SERIALD_BIND':'127.0.0.1','SERIALD_PORT':port,'SERIALD_AUTH_TOKEN':fixture.owner_token})
            opener=build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
            # Follow the existing share redirect and then access authenticated API.
            with opener.open(result['urls'][0],timeout=3) as response:
                self.assertEqual(response.status,200)
            with opener.open(fixture.url+'/api/access',timeout=3) as response:
                self.assertFalse(json.load(response)['open_access'])
            self.assertNotIn(fixture.owner_token,json.dumps(result))
