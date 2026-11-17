"""Human-owned directory inputs and editable copies; reuse sealed artifacts/tasks."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time

from .archive_io import open_archive
from .flash_runner import _json
from .results import response
from .tasks import TaskError

MAX_MEMBERS = 32768


def origin_path(artifact_path):
    return Path(artifact_path).with_suffix('.directory.json')


def public_directory_info(artifact_path):
    sidecar = origin_path(artifact_path)
    try:
        if not sidecar.exists():
            return {}
        if sidecar.is_symlink() or sidecar.stat().st_size > 8 * 1024 ** 2:
            return {'directory':True}
        document = _json(sidecar.read_bytes())
        sizes = [item['size'] for item in document['files']]
        if any(type(value) is not int or value < 0 for value in sizes):
            return {'directory':True}
        return {'directory':True, 'directory_bytes':sum(sizes)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'directory':True}


def members(root):
    if not isinstance(root, (str, Path)) or not str(root).strip() or not Path(root).is_absolute():
        raise TaskError('请选择控制机上的绝对目录', 400)
    root = Path(root).absolute()
    if root.resolve(strict=True) != root or not root.is_dir():
        raise TaskError('目录不存在或包含符号链接', 409)
    root = root.resolve(strict=True)
    result = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise TaskError('镜像目录只能包含普通文件和目录', 409)
            if stat.S_ISREG(info.st_mode):
                result.append({'name': path.relative_to(root).as_posix(), 'size': info.st_size,
                               'modified_ns': info.st_mtime_ns})
                if len(result) > MAX_MEMBERS:
                    raise TaskError('镜像目录文件过多', 413)
    return root, sorted(result, key=lambda item: item['name'])


def sparse_copy(source, target, check=lambda: None, on_progress=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    with open_archive(source) as incoming, target.open('xb') as outgoing:
        # CoW keeps reviewed cache bytes separate from editable files. Fall back
        # to sparse copying on Windows and filesystems without clone support.
        if os.name == 'posix':
            try:
                import fcntl
                fcntl.ioctl(outgoing.fileno(), 0x40049409, incoming.fileno())
                if on_progress: on_progress(os.fstat(incoming.fileno()).st_size)
                return
            except OSError:
                outgoing.seek(0)
                outgoing.truncate()
        before = os.fstat(incoming.fileno())
        count = 0
        while True:
            check()
            data = incoming.read(1024 * 1024)
            if not data:
                break
            count += len(data)
            if on_progress: on_progress(count)
            if data.count(0) == len(data):
                outgoing.seek(len(data), 1)
            else:
                outgoing.write(data)
        outgoing.truncate(count)
        after = os.fstat(incoming.fileno())
        if before.st_size != count or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise TaskError('源文件在建立工作副本时已变化', 409)


class ImageWorkspaces:
    def __init__(self, manager):
        self.manager = manager

    def register(self, directory, name=None):
        from .image_preparation import for_manager
        root, files = members(directory)
        if not any(Path(item['name']).name == 'fastboot.yaml' for item in files):
            raise TaskError('镜像目录缺少 fastboot.yaml', 409)
        if sum(item['size'] for item in files) > 32 * 1024 ** 3:
            raise TaskError('镜像目录超过展开容量限制', 413)
        # This small sealed descriptor identifies a directory revision. Only its
        # server-created sidecar enables prepare-directory; uploaded JSON cannot.
        document = {'version': 1, 'directory': str(root), 'files': files}
        data = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
        if len(data) > 8 * 1024 ** 2:
            raise TaskError('目录清单过大', 413)
        artifact = self.manager.artifacts.create('upload', (name or root.name)[:220] + '.directory', len(data), 'web', '')
        artifact_id = artifact['id']
        try:
            self.manager.artifacts.append(artifact_id, 0, data)
            artifact = self.manager.artifacts.seal(artifact_id)
            sidecar = origin_path(self.manager.artifacts._path(artifact_id, '.bin'))
            fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(document, stream)
            artifact['directory'] = True
            artifact['directory_bytes'] = sum(item['size'] for item in files)
            task = for_manager(self.manager).after_upload(artifact)
            return {'artifact': artifact, 'preparation_task': task}
        except BaseException:
            # Keep any started job's source; otherwise remove incomplete input.
            if self.manager.artifacts.get(artifact_id)['state'] != 'claimed':
                self.manager.artifacts.remove(artifact_id)
            raise

    def locate(self, artifact_id):
        from .flash_tasks import _services
        row = self.manager.artifacts.get(artifact_id, 'web')
        _, executor = _services(self.manager)
        sidecar = origin_path(self.manager.artifacts._path(artifact_id, '.bin'))
        if sidecar.exists():
            if sidecar.is_symlink() or sidecar.stat().st_size > 8 * 1024 ** 2:
                raise TaskError('镜像目录来源记录无效', 409)
            document = _json(sidecar.read_bytes())
            root, files = members(document['directory'])
            return row, executor, root, files, True
        workspace = executor.image_workspace(row)
        receipt = workspace / ('prepared-' + row['sha256'] + '.json')
        if receipt.is_symlink() or receipt.stat().st_size > 16 * 1024 ** 2:
            raise TaskError('镜像尚未准备或准备记录已变化', 409)
        document = _json(receipt.read_bytes())
        root = Path(document['root_path'])
        if not root.is_relative_to(workspace) or root.resolve(strict=True) != root:
            raise TaskError('镜像工作目录已变化', 409)
        root, files = members(root)
        return row, executor, root, files, False

    @staticmethod
    def selected(root, relative):
        if not isinstance(relative, str) or not relative or '\\' in relative:
            raise TaskError('invalid image member')
        part = Path(relative)
        if part.is_absolute() or any(value in ('.', '..') for value in part.parts):
            raise TaskError('invalid image member')
        path = root / part
        if path.resolve(strict=True) != path or not path.is_file():
            raise TaskError('镜像文件不存在或已变化', 409)
        return path

    def request(self, payload, actor='web'):
        try:
            if actor != 'web':
                raise TaskError('只有网页所有者可以访问控制机镜像目录', 403)
            if not isinstance(payload, dict) or set(payload) - {'action','id','directory','name','size','path','text','replacement_id','revision','actor','client_id','request_id'}:
                raise TaskError('invalid image workspace request')
            action = payload.get('action', 'list')
            digest = None
            if not isinstance(payload.get('text', ''), str):
                raise TaskError('无效的文本文件内容', 400)
            if action in ('save','replace','fork') and payload.get('request_id'):
                digest = self.manager.tasks.store.request_digest({**{key:value for key,value in payload.items() if key!='text'},
                    'text_sha256':hashlib.sha256(payload.get('text','').encode()).hexdigest()})
                previous = self.manager.tasks.store.find_request('web', payload['request_id'], digest)
                if previous:
                    return response(202, {'ok':True,'task':self.manager.tasks.store.get(previous)})
            if action == 'directories':
                value = payload.get('directory') or str(Path(self.manager.artifacts.root).parent)
                root = Path(value)
                if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
                    raise TaskError('请选择可访问的控制机绝对目录', 400)
                directories = []
                for child in sorted(root.iterdir(), key=lambda item:item.name.casefold()):
                    if child.is_dir() and not child.is_symlink():
                        directories.append({'name':child.name, 'path':str(child)})
                        if len(directories) >= 1000: break
                return response(200, {'ok':True,'directory':str(root),'parent':str(root.parent),'directories':directories})
            if action == 'stage':
                return response(200, {'ok':True, 'artifact':self.manager.artifacts.create('upload', payload.get('name'), payload.get('size'), 'web', '')})
            if action == 'import':
                return response(202, dict(ok=True, **self.register(payload.get('directory'), payload.get('name'))))
            row, executor, root, files, editable = self.locate(payload.get('id'))
            revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
            if action == 'list':
                return response(200, {'ok':True, 'directory':str(root), 'editable':editable,
                                      'revision':revision, 'files':files})
            if payload.get('revision') and payload['revision'] != revision:
                raise TaskError('目录已变化，请刷新文件列表后重试', 409)
            path = self.selected(root, payload.get('path')) if action != 'fork' else None
            if action == 'read':
                if path.stat().st_size > 1024 ** 2:
                    raise TaskError('文件过大，请下载或通过控制机检查', 413)
                with open_archive(path) as source:
                    content = source.read(1024 ** 2 + 1).decode('utf-8')
                return response(200, {'ok':True, 'text':content})
            if action not in ('save', 'replace', 'fork'):
                raise TaskError('invalid image workspace action')
            if action == 'save':
                content = payload.get('text')
                if not isinstance(content, str) or len(content.encode()) > 1024 ** 2:
                    raise TaskError('无效的文本文件内容', 413)
                if path.suffix == '.json':
                    json.loads(content)
            elif action == 'replace':
                replacement = self.manager.artifacts.get(payload.get('replacement_id'), 'web')
                if replacement['state'] != 'ready':
                    raise TaskError('替换文件尚未上传完成', 409)
            from .image_preparation import for_manager
            latest = for_manager(self.manager).latest(row['id'])
            if latest and (latest['state'] in ('queued','running') or latest.get('flash', {}).get('recovery_required')):
                raise TaskError('镜像正在准备或等待恢复，请先完成原任务', 423)
            from .board_maintenance import maintenance_for
            maintenance = maintenance_for(self.manager)
            maintenance.check_preparation()
            def modify(context):
                # Even prepared cache files are never edited in place. A new
                # directory revision becomes a new sealed input and preview.
                target = root
                created = False
                try:
                    with executor.image_guard(row, context.check):
                        if members(root)[1] != files:
                            raise TaskError('目录已变化，请刷新后重试', 409)
                        if not editable or action == 'fork':
                            base = executor._directory(executor.root / 'workdirs')
                            budget = executor._cache_size(root)
                            if (shutil.disk_usage(base).free < budget + 256 * 1024 ** 2
                                    or executor._cache_size(executor.root) + budget > 64 * 1024 ** 3):
                                raise TaskError('控制机空间不足，无法建立镜像工作副本', 503)
                            target = Path(tempfile.mkdtemp(prefix='image-', dir=base))
                            created = True
                            if os.name == 'posix': os.chmod(target, 0o770)
                            copied, last = 0, [0.0]
                            for item in files:
                                context.check()
                                def progress(done):
                                    now = time.monotonic()
                                    if now - last[0] >= .2 or done == item['size']:
                                        context.progress(copied + done, budget, 'copying-image-directory')
                                        last[0] = now
                                sparse_copy(root / item['name'], target / item['name'], context.check, progress)
                                copied += item['size']
                                if os.name == 'posix':
                                    member = target / item['name']
                                    os.chmod(member, 0o660)
                                    for parent in member.parents:
                                        if parent == target: break
                                        os.chmod(parent, 0o770)
                        if action == 'fork':
                            result = self.register(str(target), root.name + '-work')
                            created = False
                            return result
                        destination = target / path.relative_to(root)
                        fd, temporary = tempfile.mkstemp(prefix='.replacing-', dir=destination.parent)
                        os.close(fd)
                        temporary = Path(temporary)
                        try:
                            if action == 'save':
                                temporary.write_bytes(content.encode('utf-8'))
                                checksum = hashlib.sha256(content.encode()).hexdigest()
                            else:
                                with self.manager.artifacts.open(replacement['id']) as incoming, temporary.open('wb') as outgoing:
                                    checksum = hashlib.sha256()
                                    while True:
                                        context.check()
                                        data = incoming.read(1024 ** 2)
                                        if not data:
                                            break
                                        outgoing.write(data)
                                        checksum.update(data)
                                    checksum = checksum.hexdigest()
                                if checksum != replacement['sha256']:
                                    raise TaskError('替换文件 SHA-256 已变化', 409)
                            os.replace(temporary, destination)
                            if os.name == 'posix' and target.is_relative_to(executor.root / 'workdirs'): os.chmod(destination, 0o660)
                        finally:
                            temporary.unlink(missing_ok=True)
                        sums = next((parent / 'SHA256SUMS' for parent in [destination.parent, *destination.parents] if parent.is_relative_to(target) and (parent / 'SHA256SUMS').is_file()), target / 'SHA256SUMS')
                        if sums.is_file():
                            relative = destination.relative_to(sums.parent).as_posix()
                            lines = sums.read_text().splitlines()
                            lines = [checksum + line[64:] if len(line) >= 66 and line[64:].strip().lstrip('*').removeprefix('./') == relative else line for line in lines]
                            sums.write_text('\n'.join(lines) + '\n')
                        result = self.register(str(target), root.name + '-edited')
                        created = False
                        return result
                except BaseException:
                    if created:
                        shutil.rmtree(target, ignore_errors=True)
                    raise
            task = self.manager.tasks.start('image-prepare', 'web', payload.get('client_id',''),
                [{'serial':'','connection_id':'image-workspace:'+row['id']}], modify,
                request_id=payload.get('request_id'), input_digest=digest)
            return response(202, {'ok':True,'task':task})
        except (TaskError, ValueError, OSError, TypeError) as error:
            return response(getattr(error,'status',400), {'ok':False,'error':str(error)})
