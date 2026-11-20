#!/usr/bin/env python3
"""Refuse to publish commits that carry private addresses, internal hosts or credentials.

main is the public branch: every pushed commit, file and message is published and
cannot be withdrawn by rewriting history. This scanner checks what a push would add:
lines added by each commit (merges as combined diffs), printable strings in added
binary files, path names, commit and tag messages, and author/committer identities.

Usage:
  check_public_push.py REV...                 scan commits selected by git log REV...
  check_public_push.py --pre-push REMOTE URL  pre-push hook mode; refs arrive on stdin

Reviewed, already public values live in tools/public_push_allowlist.txt. Internal
domain patterns must not be committed: they are read from
$GIT_COMMON_DIR/info/public-push-deny and DECKLAB_PUBLIC_DENY (one regex per line).
"""
import argparse
import fnmatch
import hashlib
import ipaddress
import math
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / 'tools' / 'public_push_allowlist.txt'
ZERO = re.compile(r'^0+$')
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
IPV4 = re.compile(r'(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?!\.?\d)')
HOST_REF = re.compile(r'(?:[a-z][a-z0-9+.-]*://(?:[^/\s@\'"]*@)?|\b[\w.-]+@)'
                      r'([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)', re.I)
INTERNAL_SUFFIX = ('.internal', '.intranet', '.corp', '.lan', '.localdomain', '.home.arpa')
CREDENTIALS = (
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})')),
    ('gitlab-token', re.compile(r'\bgl(?:pat|dt|rt|cbt|ptt|ft|imt|agent)-[A-Za-z0-9_-]{20,}')),
    ('slack-token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}')),
    ('aws-access-key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('private-key', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('url-credentials', re.compile(r'\b[a-z][a-z0-9+.-]*://[^/\s:@\'"]+:([^/\s@\'"]{6,})@[\w.-]+', re.I)),
)
ASSIGNMENT = re.compile(
    r'(?i)\b[\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key|private[_-]?key|credential)s?\b'
    r'["\']?\s*(?:[:=]|=>)\s*["\']?([A-Za-z0-9+/_.~-]{20,}={0,2})')
STRINGS = re.compile(rb'[\x20-\x7e]{6,}')


class Finding:
    def __init__(self, commit, where, rule, value, path=None):
        self.commit, self.where, self.rule, self.value, self.path = commit, where, rule, value, path

    def shown(self, redact=False):
        value = self.value
        if redact:
            value = '<redacted>'
        elif self.rule not in ('private-ip', 'internal-host', 'deny-pattern', 'author-email'):
            value = value[:4] + '…' + value[-2:] if len(value) > 8 else '…'
        return f'{self.commit[:12]} {self.where}: {self.rule} {value!r} (fingerprint {fingerprint(self.value)})'


class Allowlist:
    def __init__(self, text=''):
        self.networks, self.hosts, self.emails, self.fingerprints, self.paths = [], [], [], set(), []
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            kind, _, value = line.partition(' ')
            value = value.strip()
            if not value:
                raise ValueError(f'allowlist line {number}: missing value')
            if kind == 'ip':
                self.networks.append(ipaddress.ip_network(value, strict=False))
            elif kind == 'host':
                self.hosts.append(value.lower())
            elif kind == 'email':
                self.emails.append(value.lower())
            elif kind == 'fingerprint':
                self.fingerprints.add(value.lower())
            elif kind == 'path':
                rule, _, pattern = value.partition(' ')
                if not pattern.strip():
                    raise ValueError(f'allowlist line {number}: path needs RULE GLOB')
                self.paths.append((rule, pattern.strip()))
            else:
                raise ValueError(f'allowlist line {number}: unknown entry {kind!r}')

    @classmethod
    def load(cls, path=ALLOWLIST):
        path = Path(path)
        return cls(path.read_text(encoding='utf-8') if path.exists() else '')

    def ip(self, address):
        return any(address in network for network in self.networks)

    def host(self, host):
        return any(fnmatch.fnmatchcase(host.lower(), pattern) for pattern in self.hosts)

    def email(self, email):
        return any(fnmatch.fnmatchcase(email.lower(), pattern) for pattern in self.emails)

    def path(self, rule, path):
        return path is not None and any(r in (rule, '*') and fnmatch.fnmatchcase(path, g)
                                        for r, g in self.paths)


def entropy(text):
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def load_deny(path=None, environ=os.environ):
    lines = []
    if path and Path(path).exists():
        lines += Path(path).read_text(encoding='utf-8').splitlines()
    lines += environ.get('DECKLAB_PUBLIC_DENY', '').splitlines()
    patterns = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            patterns.append(re.compile(line, re.I))
    return patterns


def scan_text(text, allow, deny, binary=False):
    """Return (rule, value) pairs for one line or extracted string."""
    hits = []
    for pattern in deny:
        for match in pattern.finditer(text):
            hits.append(('deny-pattern', match.group(0)))
    for match in IPV4.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if any(address in network for network in PRIVATE) and not allow.ip(address):
            hits.append(('private-ip', match.group(1)))
    for match in HOST_REF.finditer(text):
        host = match.group(1).lower().rstrip('.')
        if host.endswith(INTERNAL_SUFFIX) and not allow.host(host):
            hits.append(('internal-host', host))
    for rule, pattern in CREDENTIALS:
        for match in pattern.finditer(text):
            hits.append((rule, match.group(match.lastindex or 0)))
    if not binary:
        for match in ASSIGNMENT.finditer(text):
            value = match.group(1)
            if (entropy(value) >= 3.5 and re.search(r'\d', value) and re.search(r'[A-Za-z]', value)
                    and not re.fullmatch(r'[0-9a-f]{40,64}', value)):
                hits.append(('secret-assignment', value))
    # A private-key header is identical for every key, so it is only allowed by path.
    return [(rule, value) for rule, value in hits
            if rule == 'private-key' or fingerprint(value) not in allow.fingerprints]


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8', 'replace')).hexdigest()[:16]


def git(*args, cwd=None, data=None):
    result = subprocess.run(('git',) + args, cwd=cwd, input=data, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f'git {" ".join(args)} failed: {result.stderr.decode(errors="replace").strip()}')
    return result.stdout


def scan_commits(revs, allow, deny, cwd=None):
    """Scan commits selected by git log revs; return a list of findings."""
    findings = []
    meta = git('log', '--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01', *revs, '--', cwd=cwd)
    commits = []
    for record in meta.decode('utf-8', 'replace').split('\x01'):
        record = record.lstrip('\n')
        if not record:
            continue
        sha, author, author_email, committer, committer_email, body = record.split('\x00', 5)
        commits.append(sha)
        for kind, email in (('author', author_email), ('committer', committer_email)):
            if not allow.email(email):
                findings.append(Finding(sha, kind, 'author-email', email))
        for name, value in (('author name', author), ('committer name', committer)):
            findings += [Finding(sha, name, r, v) for r, v in scan_text(value, allow, deny)]
        for number, line in enumerate(body.splitlines(), 1):
            findings += [Finding(sha, f'message:{number}', r, v) for r, v in scan_text(line, allow, deny)]
    if not commits:
        return findings
    findings += scan_diffs(revs, allow, deny, cwd)
    findings += scan_binaries(revs, allow, deny, cwd)
    return [f for f in findings if not allow.path(f.rule, f.path)]


def scan_diffs(revs, allow, deny, cwd):
    findings, sha, path, line_no, parents, header = [], '', '', 0, 1, False
    output = git('log', '-p', '--cc', '-U0', '--no-color', '--no-ext-diff', '--format=%x00commit %H',
                 *revs, '--', cwd=cwd)
    for raw in output.decode('utf-8', 'replace').splitlines():
        if raw.startswith('\x00commit '):
            sha, header = raw.split()[1], False
        elif raw.startswith('diff '):
            header, path = True, ''
        elif header and raw.startswith('+++ '):
            path = raw[4:]
            path = path[2:] if path.startswith('b/') else path
            if path != '/dev/null':
                findings += [Finding(sha, 'path ' + path, r, v, path) for r, v in scan_text(path, allow, deny)]
        elif raw.startswith('@@'):
            header = False
            parents = len(raw) - len(raw.lstrip('@')) - 1
            match = re.search(r' \+(\d+)(?:,\d+)? @@', raw)
            line_no = int(match.group(1)) if match else 0
        elif not header and sha and path:
            prefix, text = raw[:parents], raw[parents:]
            if '-' in prefix:
                continue
            if '+' in prefix:
                findings += [Finding(sha, f'{path}:{line_no}', r, v, path) for r, v in scan_text(text, allow, deny)]
            line_no += 1
    return findings


def scan_binaries(revs, allow, deny, cwd):
    findings, seen, sha = [], set(), ''
    output = git('log', '--raw', '--no-abbrev', '--no-renames', '--format=%x00commit %H', *revs, '--', cwd=cwd)
    blobs = []
    for raw in output.decode('utf-8', 'replace').splitlines():
        if raw.startswith('\x00commit '):
            sha = raw.split()[1]
        elif raw.startswith(':'):
            fields, _, path = raw.partition('\t')
            parts = fields.split()
            blob, status = parts[3], parts[4]
            if status[0] in 'AM' and not ZERO.match(blob) and blob not in seen:
                seen.add(blob)
                blobs.append((sha, path, blob))
    for sha, path, blob in blobs:
        data = git('cat-file', 'blob', blob, cwd=cwd)
        if b'\0' not in data[:8192]:
            continue
        for match in STRINGS.finditer(data):
            text = match.group(0).decode('ascii')
            findings += [Finding(sha, f'{path} (binary)', r, v, path)
                         for r, v in scan_text(text, allow, deny, binary=True)]
    return findings


def scan_tag(ref, sha, allow, deny, cwd=None):
    if git('cat-file', '-t', sha, cwd=cwd).strip() != b'tag':
        return []
    text = git('cat-file', 'tag', sha, cwd=cwd).decode('utf-8', 'replace')
    header, _, body = text.partition('\n\n')
    findings = []
    tagger = re.search(r'^tagger .*<([^>]*)>', header, re.M)
    if tagger and not allow.email(tagger.group(1)):
        findings.append(Finding(sha, ref + ' tagger', 'author-email', tagger.group(1)))
    body = body.split('-----BEGIN PGP SIGNATURE-----', 1)[0]
    for number, line in enumerate(body.splitlines(), 1):
        findings += [Finding(sha, f'{ref} message:{number}', r, v) for r, v in scan_text(line, allow, deny)]
    return findings


def pre_push_revs(remote, updates, cwd=None):
    """Translate pre-push stdin lines into (ref, local sha, git log revs) to scan."""
    plans, blocked = [], []
    for line in updates:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if ZERO.match(local_sha):
            continue  # deleting a remote ref publishes nothing new
        if remote_ref.startswith('refs/tags/archive/') or local_ref.startswith('refs/tags/archive/'):
            blocked.append(f'{remote_ref}: archive tags stay local and are never pushed')
            continue
        known = not ZERO.match(remote_sha) and subprocess.run(
            ('git', 'cat-file', '-e', remote_sha + '^{commit}'), cwd=cwd, capture_output=True).returncode == 0
        if known:
            revs = [remote_sha + '..' + local_sha]
        else:
            revs = [local_sha, '--not', f'--remotes={remote}']
        plans.append((remote_ref, local_sha, revs))
    return plans, blocked


def deny_path(cwd=None):
    try:
        common = git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=cwd).decode().strip()
    except RuntimeError:
        return None
    return Path(common) / 'info' / 'public-push-deny'


def report(findings, blocked, deny_file, deny, redact=False):
    for item in blocked:
        print('BLOCKED ' + item, file=sys.stderr)
    unique = {}
    for finding in findings:
        unique.setdefault((finding.commit, finding.where, finding.rule, finding.value), finding)
    for finding in unique.values():
        print('HIT ' + finding.shown(redact), file=sys.stderr)
    if not deny:
        print(f'warning: no internal domain patterns loaded ({deny_file} or DECKLAB_PUBLIC_DENY)', file=sys.stderr)
    if unique or blocked:
        print(f'public push check failed: {len(unique)} finding(s). main is public; anything pushed '
              'stays public even after force push. Remove the content and rewrite the unpushed commits; '
              'add a tools/public_push_allowlist.txt entry only for reviewed public values.', file=sys.stderr)
        return 1
    print('public push check passed', file=sys.stderr)
    return 0


def main(argv=None, stdin=sys.stdin):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pre-push', nargs='+', metavar=('REMOTE', 'URL'))
    parser.add_argument('--allowlist', default=str(ALLOWLIST))
    parser.add_argument('--deny-file')
    parser.add_argument('--redact', action='store_true',
                        help='print locations and fingerprints only; use where the log is public (CI)')
    parser.add_argument('revs', nargs='*')
    args = parser.parse_args(argv)
    allow = Allowlist.load(args.allowlist)
    deny_file = args.deny_file or deny_path()
    deny = load_deny(deny_file)
    findings, blocked = [], []
    if args.pre_push:
        plans, blocked = pre_push_revs(args.pre_push[0], stdin.read().splitlines())
        for ref, sha, revs in plans:
            if ref.startswith('refs/tags/'):
                findings += scan_tag(ref, sha, allow, deny)
            findings += scan_commits(revs, allow, deny)
    elif args.revs:
        findings = scan_commits(args.revs, allow, deny)
    else:
        parser.error('give revisions to scan or --pre-push REMOTE URL')
    return report(findings, blocked, deny_file, deny, args.redact)


if __name__ == '__main__':
    sys.exit(main())
