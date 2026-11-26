"""Check and install a versioned package from the public DeckLab release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__


PROJECT = 'https://github.com/qianqiu-2020/decklab'
LATEST = 'https://api.github.com/repos/qianqiu-2020/decklab/releases/latest'
VERSION = re.compile(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}')
MAX_PACKAGE = 512 * 1024 * 1024


class UpdateError(ValueError):
    pass


def version_key(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise UpdateError('无法识别发布版本号')
    return tuple(map(int, value.split('.')))


def platform_name():
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        return ''
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform.startswith('linux'):
        return 'linux'
    return ''


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if (url.scheme != 'https' or url.netloc not in (
                'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
            raise UpdateError('GitHub 返回了不受支持的下载跳转')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_github(url):
    request = Request(url, headers={'User-Agent': 'DeckLab/' + __version__,
                                   'Accept': 'application/vnd.github+json' if url == LATEST else '*/*'})
    return build_opener(GitHubRedirects()).open(request, timeout=12)


def check_update(current=__version__, target=None):
    target = platform_name() if target is None else target
    try:
        with open_github(LATEST) as response:
            release = json.loads(response.read(1024 * 1024))
    except (OSError, URLError, ValueError) as error:
        raise UpdateError('无法读取 GitHub 正式版本，请检查控制机网络后重试') from error
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise UpdateError('GitHub 未返回正式版本')
    tag = release.get('tag_name')
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise UpdateError('无法识别 GitHub 发布标签')
    latest = tag[1:]
    available = version_key(latest) > version_key(current)
    result = {'current': current, 'latest': latest, 'available': available, 'platform': target,
              'release_url': PROJECT + '/releases/tag/' + tag, 'package': None}
    packages = {'linux': f'decklab_{latest}_amd64.deb',
                'windows': f'decklab-{latest}-windows-x64-setup.exe'}
    name = packages.get(target)
    assets = release.get('assets')
    if not name or not isinstance(assets, list):
        return result
    url = f'{PROJECT}/releases/download/{tag}/{name}'
    for asset in assets:
        if not isinstance(asset, dict) or asset.get('name') != name or asset.get('state') != 'uploaded':
            continue
        digest, size = asset.get('digest'), asset.get('size')
        if (asset.get('browser_download_url') == url and isinstance(digest, str)
                and re.fullmatch(r'sha256:[0-9a-f]{64}', digest) and type(size) is int and 0 < size <= MAX_PACKAGE):
            result['package'] = {'name': name, 'url': url, 'sha256': digest[7:], 'size': size}
    return result


def download_package(package, destination):
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 900
    with open_github(package['url']) as response, destination.open('xb') as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > package['size'] or time.monotonic() > deadline:
                raise UpdateError('安装包超过公布的大小或下载超时')
            digest.update(block)
            output.write(block)
    if size != package['size'] or digest.hexdigest() != package['sha256']:
        raise UpdateError('安装包 SHA-256 或大小与 GitHub 记录不一致')


def install_linux(package, version):
    installed = subprocess.check_output(
        ['/usr/bin/dpkg-query', '-W', '-f=${Version}', 'decklab'], text=True).strip()
    subprocess.run(['/usr/bin/dpkg', '--compare-versions', version, 'gt', installed], check=True)
    with tempfile.TemporaryDirectory(prefix='decklab-update-', dir='/var/tmp') as directory:
        destination = Path(directory) / package['name']
        download_package(package, destination)
        for field, expected in (('Package', 'decklab'), ('Version', version), ('Architecture', 'amd64')):
            actual = subprocess.check_output(['/usr/bin/dpkg-deb', '-f', str(destination), field], text=True).strip()
            if actual != expected:
                raise UpdateError('Debian 包名、版本或架构与所选更新不符')
        subprocess.run(['/usr/bin/dpkg', '-i', str(destination)], check=True)


def install_windows(package):
    # ShellExecute hands the verified file to the normal Windows installer.
    # Keep the temporary installer available after this process exits.
    directory = Path(tempfile.mkdtemp(prefix='decklab-update-'))
    destination = directory / package['name']
    try:
        download_package(package, destination)
        os.startfile(str(destination), 'runas')
    except BaseException:
        destination.unlink(missing_ok=True)
        directory.rmdir()
        raise
    print('已打开安装器，请在本机确认 Windows 安装提示。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', nargs='?', help='install this exact version, or omit to check')
    args = parser.parse_args(argv)
    try:
        result = check_update()
        if args.version is None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        version_key(args.version)
        if not result['available'] or args.version != result['latest'] or not result['package']:
            raise UpdateError('该版本无法安装，请重新检查 GitHub 更新')
        if result['platform'] == 'linux':
            if os.geteuid() != 0:
                raise UpdateError('请在控制机上使用 sudo decklab-update 安装')
            install_linux(result['package'], args.version)
        elif result['platform'] == 'windows':
            install_windows(result['package'])
        return 0
    except (OSError, UpdateError, subprocess.CalledProcessError) as error:
        print(f'DeckLab: {error}', file=sys.stderr)
        return 1
