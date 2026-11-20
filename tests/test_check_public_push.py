import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import check_public_push as scanner

# Sensitive-looking samples are assembled at run time so this file never carries them.
PRIVATE_IP = '.'.join(('10', '77', '3', '21'))
TEST_IP = '.'.join(('10', '0', '0', '2'))
GITHUB_TOKEN = 'gh' + 'p_' + 'aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5'
AWS_KEY = 'AK' + 'IA' + 'Q7ZK3M5N7P9R2T4V'
KEY_HEADER = '-----BEGIN ' + 'RSA PRIVATE KEY-----'
INTERNAL_HOST = 'build.' + 'corp'
EMAIL = 'dev@example.test'
NET_B = '.'.join(('172', '20', '1', '5'))
NET_C = '.'.join(('192', '168', '4', '9'))
SECRET = 'q8Vn2LxR' + '7tKp4Wz9Hc3Jm6Fd'
URL_PASSWORD = 'Zx81' + 'kQp2Lm'
ALLOW = f'email {EMAIL}\nip {TEST_IP}\nhost *.example.lan\n'


def hits(text, allow=ALLOW, deny=()):
    return scanner.scan_text(text, scanner.Allowlist(allow), list(deny))


class ScanTextTests(unittest.TestCase):
    def test_private_addresses_hit_unless_allowlisted(self):
        self.assertEqual(hits(f'host = "{PRIVATE_IP}"'), [('private-ip', PRIVATE_IP)])
        self.assertEqual(hits(f'host = "{TEST_IP}"'), [])
        self.assertEqual(hits(f'ip {NET_B} and {NET_C}'), [('private-ip', NET_B), ('private-ip', NET_C)])
        allow = ALLOW + f'ip {NET_B}/16\n'
        self.assertEqual(hits(NET_B, allow), [])

    def test_public_addresses_and_version_numbers_do_not_hit(self):
        for text in ('8.8.8.8', '172.32.0.1', '11.0.0.1', 'version 1.10.0.3.4', 'v1.10.0.3', '127.0.0.1'):
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])

    def test_credentials_hit(self):
        self.assertEqual(hits(f'token: {GITHUB_TOKEN}')[0][0], 'github-token')
        self.assertEqual(hits(f'key={AWS_KEY}')[0][0], 'aws-access-key')
        self.assertEqual(hits(KEY_HEADER), [('private-key', KEY_HEADER)])
        self.assertEqual(hits('url = "https://deploy:' + URL_PASSWORD + '@example.test/repo"'),
                         [('url-credentials', URL_PASSWORD)])
        self.assertEqual(hits(f'API_TOKEN = "{SECRET}"'), [('secret-assignment', SECRET)])

    def test_low_entropy_or_digest_assignments_do_not_hit(self):
        for text in ('token = "aaaaaaaaaaaaaaaaaaaaaaaa"', 'password = placeholder_password_value',
                     'secret_sha256 = "' + 'ab12' * 16 + '"', 'token = request.headers.get("x")'):
            with self.subTest(text=text):
                self.assertEqual(hits(text), [])

    def test_internal_hosts_need_a_host_context(self):
        self.assertEqual(hits(f'clone ssh://git@{INTERNAL_HOST}/team/repo'), [('internal-host', INTERNAL_HOST)])
        self.assertEqual(hits('state = s.internal; data.lan = 1'), [])
        self.assertEqual(hits('https://backend.example.lan:8899'), [])

    def test_deny_patterns_from_file_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'deny'
            path.write_text('# comment\n(?<![\\w-])secret-co\\.example\\b\n', encoding='utf-8')
            deny = scanner.load_deny(path, {'DECKLAB_PUBLIC_DENY': 'hidden-team'})
        self.assertEqual(len(deny), 2)
        self.assertEqual(hits('see git.SECRET-CO.example/x', deny=deny), [('deny-pattern', 'SECRET-CO.example')])
        self.assertEqual(hits('the hidden-team wiki', deny=deny), [('deny-pattern', 'hidden-team')])
        self.assertEqual(hits('notsecret-co.example', deny=deny), [])

    def test_fingerprint_allows_a_reviewed_value_but_not_a_key_header(self):
        secret = SECRET
        allow = ALLOW + f'fingerprint {scanner.fingerprint(secret)}\nfingerprint {scanner.fingerprint(KEY_HEADER)}\n'
        self.assertEqual(hits(f'token = "{secret}"', allow), [])
        self.assertEqual(hits(KEY_HEADER, allow), [('private-key', KEY_HEADER)])

    def test_allowlist_rejects_unknown_or_incomplete_entries(self):
        for text in ('domain x', 'ip', 'path private-key'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                scanner.Allowlist(text)

    def test_repository_allowlist_parses_and_keeps_publishing_identities(self):
        allow = scanner.Allowlist.load()
        self.assertTrue(allow.emails)
        self.assertTrue(allow.path('private-key', 'tests/fixtures/backend-tls/public-test-key.pem'))
        self.assertFalse(allow.path('private-key', 'docs/key.pem'))


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        environment = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
                       'GIT_AUTHOR_NAME': 'Dev', 'GIT_AUTHOR_EMAIL': EMAIL,
                       'GIT_COMMITTER_NAME': 'Dev', 'GIT_COMMITTER_EMAIL': EMAIL}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.git('init', '-q', '-b', 'main')
        self.commit('README.md', 'public\n', 'docs: start')
        self.base = self.git('rev-parse', 'HEAD')
        self.allow = scanner.Allowlist(ALLOW)

    def git(self, *args):
        return subprocess.run(('git',) + args, cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, name, content, message, binary=False):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            path.write_bytes(content)
        else:
            path.write_text(content, encoding='utf-8')
        self.git('add', name)
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def scan(self, *revs):
        return scanner.scan_commits(list(revs), self.allow, [], cwd=self.repo)


class GitScanTests(GitRepoCase):
    def test_clean_range_passes(self):
        self.commit('app.py', f'HOST = "{TEST_IP}"\n', 'app: add placeholder')
        self.assertEqual(self.scan(self.base + '..HEAD'), [])

    def test_added_lines_report_path_and_line(self):
        sha = self.commit('app.py', f'one\nHOST = "{PRIVATE_IP}"\n', 'app: add host')
        [finding] = self.scan(self.base + '..HEAD')
        self.assertEqual((finding.commit, finding.where, finding.rule, finding.value),
                         (sha, 'app.py:2', 'private-ip', PRIVATE_IP))

    def test_removed_lines_and_already_public_commits_are_not_rescanned(self):
        self.commit('app.py', f'HOST = "{PRIVATE_IP}"\n', 'app: leak')
        leaked = self.git('rev-parse', 'HEAD')
        self.commit('app.py', 'HOST = None\n', 'app: remove')
        self.assertEqual(self.scan(leaked + '..HEAD'), [])
        self.assertEqual(len(self.scan(self.base + '..HEAD')), 1)

    def test_commit_message_and_identity_are_scanned(self):
        self.git('commit', '-q', '--allow-empty', '-m', f'ops: rotate\n\nold {GITHUB_TOKEN}',
                 '--author', 'Dev <dev@other.example>')
        rules = sorted((f.where, f.rule) for f in self.scan(self.base + '..HEAD'))
        self.assertEqual(rules, [('author', 'author-email'), ('message:3', 'github-token')])

    def test_binary_strings_are_scanned(self):
        self.commit('loader.bin', b'\x00\x01ipaddr=' + PRIVATE_IP.encode() + b'\x00\xff', 'loader: add',
                    binary=True)
        [finding] = self.scan(self.base + '..HEAD')
        self.assertEqual((finding.where, finding.rule), ('loader.bin (binary)', 'private-ip'))
        allow = scanner.Allowlist(ALLOW + 'path private-ip loader.bin\n')
        self.assertEqual(scanner.scan_commits([self.base + '..HEAD'], allow, [], cwd=self.repo), [])

    def test_ours_merge_adds_nothing_but_an_evil_merge_is_caught(self):
        self.git('switch', '-q', '-c', 'side')
        self.commit('side.txt', 'side\n', 'side: change')
        self.git('switch', '-q', 'main')
        self.commit('main.txt', 'main\n', 'main: change')
        start = self.git('rev-parse', 'HEAD')
        self.git('merge', '-q', '-s', 'ours', '--no-edit', 'side')
        merge = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.scan(start + '..HEAD', '--not', 'side'), [])
        self.git('reset', '-q', '--hard', start)
        self.git('merge', '-q', '--no-commit', 'side')
        (self.repo / 'side.txt').write_text(f'side\n{PRIVATE_IP}\n', encoding='utf-8')
        self.git('add', 'side.txt')
        self.git('commit', '-q', '--no-edit')
        findings = self.scan(start + '..HEAD', '--not', 'side')
        self.assertEqual([(f.where, f.rule) for f in findings], [('side.txt:2', 'private-ip')])
        self.assertNotEqual(merge, self.git('rev-parse', 'HEAD'))

    def test_pre_push_plans_ranges_and_blocks_archive_tags(self):
        head = self.git('rev-parse', 'HEAD')
        zero = '0' * 40
        lines = [f'refs/heads/main {head} refs/heads/main {self.base}',
                 f'refs/heads/new {head} refs/heads/new {zero}',
                 f'(delete) {zero} refs/heads/old {head}',
                 f'refs/tags/archive/x {head} refs/tags/archive/x {zero}',
                 f'refs/heads/main {head} refs/heads/main {"f" * 40}']
        plans, blocked = scanner.pre_push_revs('github', lines, cwd=self.repo)
        self.assertEqual([p[2] for p in plans], [[f'{self.base}..{head}'],
                                                 [head, '--not', '--remotes=github'],
                                                 [head, '--not', '--remotes=github']])
        self.assertEqual(len(blocked), 1)

    def test_pre_push_main_fails_on_a_finding_and_scans_annotated_tags(self):
        self.commit('app.py', 'ok\n', 'app: clean')
        head = self.git('rev-parse', 'HEAD')
        allow_file = self.repo / 'allow.txt'
        allow_file.write_text(ALLOW, encoding='utf-8')
        args = ['--allowlist', str(allow_file), '--deny-file', str(self.repo / 'none'), '--pre-push', 'github', 'url']
        update = f'refs/heads/main {head} refs/heads/main {self.base}\n'
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)
        with mock.patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(scanner.main(args, io.StringIO(update)), 0)
        self.git('tag', '-a', 'v1', '-m', f'release via https://{INTERNAL_HOST}/ci', 'HEAD')
        tag = self.git('rev-parse', 'v1')
        update = f'refs/tags/v1 {tag} refs/tags/v1 {"0" * 40}\n'
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(scanner.main(args, io.StringIO(update)), 1)
        self.assertIn('refs/tags/v1 message:1: internal-host', stderr.getvalue())
        sha = self.commit('app.py', f'ok\n{GITHUB_TOKEN}\n', 'app: oops')
        update = f'refs/heads/main {sha} refs/heads/main {head}\n'
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(scanner.main(args + ['--redact'], io.StringIO(update)), 1)
        self.assertIn('github-token', stderr.getvalue())
        self.assertNotIn(GITHUB_TOKEN[:8], stderr.getvalue())


@unittest.skipUnless(shutil.which('sh'), 'the hook installer needs a POSIX shell')
class HookInstallTests(GitRepoCase):
    def setUp(self):
        super().setUp()
        root = Path(scanner.__file__).resolve().parents[1]
        for name in ('tools/check_public_push.py', 'tools/install_hooks.sh', '.githooks/pre-push'):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        allow = (root / 'tools/public_push_allowlist.txt').read_text(encoding='utf-8') + ALLOW
        (self.repo / 'tools/public_push_allowlist.txt').write_text(allow, encoding='utf-8')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'tools: add public push gate')
        self.remote = self.repo.parent / (self.repo.name + '-remote.git')
        subprocess.run(('git', 'init', '-q', '--bare', str(self.remote)), check=True, capture_output=True)
        self.addCleanup(shutil.rmtree, self.remote, True)
        self.git('remote', 'add', 'public', str(self.remote))
        subprocess.run(('sh', 'tools/install_hooks.sh'), cwd=self.repo, check=True, capture_output=True)

    def push(self, cwd=None):
        return subprocess.run(('git', 'push', '-q', 'public', 'HEAD:refs/heads/main'), cwd=cwd or self.repo,
                              capture_output=True, text=True)

    def test_installed_hook_gates_every_push(self):
        self.assertTrue(self.git('config', 'core.hooksPath').endswith('hooks'))
        self.assertEqual(self.push().returncode, 0)
        self.commit('app.py', f'HOST = "{PRIVATE_IP}"\n', 'app: leak')
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('private-ip', result.stderr)
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.commit('app.py', 'HOST = None\n', 'app: clean')
        self.assertEqual(self.push().returncode, 0, self.push().stderr)

    def test_worktree_older_than_the_scanner_uses_the_installed_copy(self):
        old = self.repo.parent / (self.repo.name + '-old')
        self.git('worktree', 'add', '-q', '--detach', str(old), self.base)
        self.addCleanup(shutil.rmtree, old, True)
        (old / 'leak.txt').write_text(PRIVATE_IP + '\n', encoding='utf-8')
        subprocess.run(('git', 'add', 'leak.txt'), cwd=old, check=True)
        subprocess.run(('git', 'commit', '-q', '-m', 'old: leak'), cwd=old, check=True)
        result = self.push(old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('installed copy', result.stderr)
        self.assertIn('private-ip', result.stderr)


if __name__ == '__main__':
    unittest.main()
