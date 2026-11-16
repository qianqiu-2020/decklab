"""Native SDK package checks use synthetic binaries, never load a library."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from tools import package_desktop_runtime as runtime


def elf(machine=62):
    return b'\x7fELF\x02\x01' + bytes(10) + struct.pack('<HHI', 3, machine, 1) + bytes(40)


def pe(machine=0x8664):
    header = bytearray(64)
    header[:2] = b'MZ'
    struct.pack_into('<I', header, 60, 64)
    return bytes(header) + b'PE\0\0' + struct.pack('<H', machine) + bytes(22)


class DesktopRuntimePackageTests(unittest.TestCase):
    def fixture(self, root, platform='linux-amd64'):
        repository = root / 'repository'
        for name in runtime.SOURCES:
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('reviewed wrapper fixture ' + name)
        sources = {name: {'version': '1.0', 'url': 'https://example.test/' + name + '.tar.gz',
                          'sha256': str(index) * 64}
                   for index, name in enumerate(('freerdp', 'openssl', 'openh264'), 1)}
        reviewed = repository / 'packaging/third-party/freerdp-sources.json'
        reviewed.parent.mkdir(parents=True)
        reviewed.write_text(json.dumps({'schema': 1, 'sources': sources}))
        source = root / 'sdk'
        source.mkdir()
        binary = elf() if platform == 'linux-amd64' else pe()
        wrapper = source / runtime.WRAPPERS[platform]
        wrapper.write_bytes(binary)
        dependency = source / ('libfreerdp3.so.3' if platform == 'linux-amd64' else 'freerdp3.dll')
        dependency.write_bytes(binary)
        notices = {}
        for name in sources:
            path = source / 'third-party' / name / 'LICENSE.txt'
            path.parent.mkdir(parents=True)
            path.write_text(name + ' original license fixture')
            notices[name] = [path.relative_to(source).as_posix()]
        manifest = {'schema': 1, 'platform': platform, 'abi': 1, 'wrapper': wrapper.name,
                    'source_files': {name: runtime.digest(repository / name) for name in runtime.SOURCES},
                    'sources': sources, 'notices': notices, 'files': runtime.tree_inventory(source)}
        (source / runtime.MANIFEST).write_text(json.dumps(manifest))
        return repository, source, manifest

    def save(self, source, manifest, rehash=False):
        if rehash:
            files = runtime.tree_inventory(source)
            files.pop(runtime.MANIFEST)
            manifest['files'] = files
        (source / runtime.MANIFEST).write_text(json.dumps(manifest))

    def test_linux_and_windows_publish_exact_files_without_loading(self):
        for platform in runtime.WRAPPERS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, source, manifest = self.fixture(root, platform)
                (source / manifest['wrapper']).chmod(0o755)
                with mock.patch.object(runtime, 'ROOT', repository), \
                        mock.patch('ctypes.CDLL', side_effect=AssertionError('must not load native code')):
                    self.assertEqual(runtime.validate(source, platform), manifest)
                    destination = root / 'package' / 'desktop-runtime' / 'freerdp'
                    previous_umask = os.umask(0o077)
                    try:
                        self.assertEqual(runtime.publish(source, destination, platform), manifest)
                    finally:
                        os.umask(previous_umask)
                    self.assertEqual(runtime.tree_inventory(source), runtime.tree_inventory(destination))
                    if os.name != 'nt':
                        self.assertEqual((destination / manifest['wrapper']).stat().st_mode & 0o777, 0o644)
                        self.assertEqual((destination / 'third-party/freerdp').stat().st_mode & 0o777, 0o755)
                    with self.assertRaisesRegex(ValueError, 'already exists'):
                        runtime.publish(source, destination, platform)

    def test_manifest_rejects_wrong_platform_abi_wrapper_and_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, source, manifest = self.fixture(Path(directory))
            with mock.patch.object(runtime, 'ROOT', repository):
                for field, value in (('platform', 'windows-amd64'), ('abi', 2), ('abi', True),
                                     ('schema', True), ('wrapper', 'unreviewed.so'), ('extra', 'ignored')):
                    with self.subTest(field=field, value=value):
                        bad = dict(manifest, **{field: value})
                        self.save(source, bad)
                        with self.assertRaisesRegex(ValueError, 'mismatch'):
                            runtime.validate(source, 'linux-amd64')

    def test_upstream_and_wrapper_sources_must_match_reviewed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, source, manifest = self.fixture(Path(directory))
            with mock.patch.object(runtime, 'ROOT', repository):
                bad = copy.deepcopy(manifest)
                bad['sources']['freerdp']['sha256'] = 'f' * 64
                self.save(source, bad)
                with self.assertRaisesRegex(ValueError, 'reviewed inputs'):
                    runtime.validate(source, 'linux-amd64')
                self.save(source, manifest)
                (repository / runtime.SOURCES[1]).write_text('new wrapper implementation')
                with self.assertRaisesRegex(ValueError, 'wrapper source'):
                    runtime.validate(source, 'linux-amd64')

    def test_complete_inventory_rejects_changed_missing_and_extra_files(self):
        for mutation in ('changed', 'missing', 'extra'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                repository, source, manifest = self.fixture(Path(directory))
                library = source / 'libfreerdp3.so.3'
                if mutation == 'changed':
                    library.write_bytes(elf() + b'tampered')
                elif mutation == 'missing':
                    library.unlink()
                else:
                    (source / 'unreviewed.txt').write_text('extra payload')
                with mock.patch.object(runtime, 'ROOT', repository), \
                        self.assertRaisesRegex(ValueError, 'changed checksums'):
                    runtime.validate(source, 'linux-amd64')

    def test_inventory_paths_rows_and_notice_source_associations_are_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, source, manifest = self.fixture(Path(directory))
            bad_cases = []
            for name in ('../outside', '/absolute', 'C:/outside', 'third-party\\license'):
                bad = copy.deepcopy(manifest)
                bad['files'][name] = {'sha256': 'a' * 64, 'size': 1}
                bad_cases.append(bad)
            for row in ({'sha256': 'not-a-digest', 'size': 1}, {'sha256': 'a' * 64, 'size': True},
                        {'sha256': 'a' * 64, 'size': -1}):
                bad = copy.deepcopy(manifest)
                bad['files']['extra'] = row
                bad_cases.append(bad)
            bad = copy.deepcopy(manifest)
            bad['files'][runtime.MANIFEST] = {'sha256': 'a' * 64, 'size': 1}
            bad_cases.append(bad)
            for notices in ({}, {'freerdp': ['../outside']},
                            dict(manifest['notices'], openssl=['third-party/missing']),
                            dict(manifest['notices'], openssl=[manifest['wrapper']])):
                bad = copy.deepcopy(manifest)
                bad['notices'] = notices
                bad_cases.append(bad)
            with mock.patch.object(runtime, 'ROOT', repository):
                for index, bad in enumerate(bad_cases):
                    with self.subTest(index=index):
                        self.save(source, bad)
                        with self.assertRaises(ValueError):
                            runtime.validate(source, 'linux-amd64')

    def test_all_native_libraries_must_match_platform_architecture(self):
        for platform in runtime.WRAPPERS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                repository, source, manifest = self.fixture(Path(directory), platform)
                binary = elf(183) if platform == 'linux-amd64' else pe(0x14c)
                library = source / ('libfreerdp3.so.3' if platform == 'linux-amd64' else 'freerdp3.dll')
                library.write_bytes(binary)
                self.save(source, manifest, rehash=True)
                with mock.patch.object(runtime, 'ROOT', repository), \
                        self.assertRaisesRegex(ValueError, 'architecture'):
                    runtime.validate(source, platform)

    def test_symlinks_and_source_nested_destination_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, source, manifest = self.fixture(Path(directory))
            with mock.patch.object(runtime, 'ROOT', repository):
                with self.assertRaisesRegex(ValueError, 'inside its source'):
                    runtime.publish(source, source / 'nested', 'linux-amd64')
                try:
                    (source / 'third-party/link').symlink_to(source / manifest['wrapper'])
                except OSError:
                    return  # Windows may not grant CreateSymbolicLink to a test user.
                with self.assertRaisesRegex(ValueError, 'symbolic'):
                    runtime.validate(source, 'linux-amd64')

    def test_publish_rechecks_bytes_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, manifest = self.fixture(root)
            copyfile = runtime.shutil.copyfile
            def corrupt_copy(original, destination):
                result = copyfile(original, destination)
                if Path(destination).name == manifest['wrapper']:
                    with Path(destination).open('ab') as stream:
                        stream.write(b'changed during packaging')
                return result
            with mock.patch.object(runtime, 'ROOT', repository), \
                    mock.patch.object(runtime.shutil, 'copyfile', side_effect=corrupt_copy), \
                    self.assertRaisesRegex(ValueError, 'changed checksums'):
                runtime.publish(source, root / 'package', 'linux-amd64')

    def test_notice_inventory_preserves_sources_and_all_closure_notices(self):
        for platform in runtime.WRAPPERS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, source, manifest = self.fixture(root, platform)
                extra = source / 'third-party/closure/NOTICE.txt'
                extra.parent.mkdir()
                extra.write_text('additional reviewed closure attribution')
                self.save(source, manifest, rehash=True)
                bundle = root / 'bundle'
                sdk = bundle / runtime.RESOURCE_PATH
                sdk.parent.mkdir(parents=True)
                source.rename(sdk)
                with mock.patch.object(runtime, 'ROOT', repository), \
                        mock.patch('ctypes.CDLL', side_effect=AssertionError('must not load a library')):
                    inventory = runtime.notice_inventory(bundle, platform)
                    self.assertEqual(runtime.notice_inventory(bundle, platform, bundle / 'third-party'), inventory)
                self.assertEqual(inventory['sources'], manifest['sources'])
                self.assertEqual(inventory['manifest_sha256'], runtime.digest(sdk / runtime.MANIFEST))
                self.assertEqual(set(inventory['providers']), {
                    runtime.RESOURCE_PATH + '/' + manifest['wrapper'],
                    runtime.RESOURCE_PATH + '/' + ('libfreerdp3.so.3' if platform == 'linux-amd64' else 'freerdp3.dll')})
                self.assertEqual(inventory['notices'], {name: ['desktop-sdk/' + path for path in paths]
                    for name, paths in manifest['notices'].items()})
                self.assertIn('desktop-sdk/third-party/closure/NOTICE.txt',
                    {row['file'] for row in inventory['notice_files']})
                for row in inventory['notice_files']:
                    self.assertEqual(runtime.digest(bundle / 'third-party' / row['file']), row['sha256'])

    def test_notice_inventory_rechecks_copied_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, _ = self.fixture(root)
            sdk = root / 'bundle' / runtime.RESOURCE_PATH
            sdk.parent.mkdir(parents=True)
            source.rename(sdk)
            original = runtime.shutil.copyfile
            def changed_copy(source, target):
                original(source, target)
                with Path(target).open('ab') as stream:
                    stream.write(b'changed')
            with mock.patch.object(runtime, 'ROOT', repository), \
                    mock.patch.object(runtime.shutil, 'copyfile', side_effect=changed_copy), \
                    self.assertRaisesRegex(ValueError, 'notice changed'):
                runtime.notice_inventory(root / 'bundle', 'linux-amd64', root / 'notices')

    def test_frozen_duplicates_are_removed_but_reviewed_sdk_and_other_libraries_stay(self):
        for platform in runtime.WRAPPERS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, source, manifest = self.fixture(root, platform)
                bundle = root / 'bundle'
                sdk = bundle / runtime.RESOURCE_PATH
                sdk.parent.mkdir(parents=True)
                source.rename(sdk)
                expected = []
                for name in manifest['files']:
                    if name == manifest['wrapper'] or name.endswith(('.so.3', '.dll')):
                        alias = bundle / '_internal' / Path(name).name
                        alias.write_bytes((sdk / name).read_bytes())
                        expected.append(alias.relative_to(bundle).as_posix())
                unrelated = bundle / '_internal' / 'other-library.so'
                unrelated.write_bytes(b'requires its original source attribution')
                with mock.patch.object(runtime, 'ROOT', repository):
                    self.assertEqual(runtime.prune_frozen_aliases(bundle, platform), sorted(expected))
                    self.assertEqual(runtime.validate(sdk, platform), manifest)
                    self.assertEqual(runtime.prune_frozen_aliases(bundle, platform), [])
                self.assertTrue(unrelated.is_file())

    def test_changed_frozen_duplicate_fails_without_removing_valid_duplicates(self):
        for platform in runtime.WRAPPERS:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository, source, manifest = self.fixture(root, platform)
                bundle = root / 'bundle'
                sdk = bundle / runtime.RESOURCE_PATH
                sdk.parent.mkdir(parents=True)
                source.rename(sdk)
                original = bundle / '_internal' / manifest['wrapper']
                original.write_bytes((sdk / manifest['wrapper']).read_bytes())
                dependency = 'libfreerdp3.so.3' if platform == 'linux-amd64' else 'freerdp3.dll'
                changed = bundle / '_internal' / dependency
                changed.write_bytes((sdk / dependency).read_bytes() + b'changed')
                with mock.patch.object(runtime, 'ROOT', repository), \
                        self.assertRaisesRegex(ValueError, 'differs from reviewed SDK'):
                    runtime.prune_frozen_aliases(bundle, platform)
                self.assertTrue(original.is_file())
                self.assertTrue(changed.is_file())

    def test_notice_inventory_rejects_parent_directory_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source, _ = self.fixture(root)
            bundle = root / 'bundle'
            parent = bundle / '_internal'
            parent.mkdir(parents=True)
            try:
                (parent / 'desktop-runtime').symlink_to(source.parent, target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks require privileges on this host')
            source.rename(root / 'freerdp')
            with self.assertRaisesRegex(ValueError, 'escapes bundle'):
                runtime.notice_inventory(bundle, 'linux-amd64')


if __name__ == '__main__':
    unittest.main()
