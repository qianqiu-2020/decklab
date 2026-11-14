"""Complete install inputs are offline, architecture checked and hash pinned."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from tools.install_components import load, tree_inventory, digest, executor_metadata


class ComponentTests(unittest.TestCase):
    def bundle(self, directory):
        root = Path(directory)
        executor = root/'decklab-flash'
        executor.write_bytes(b'\x7fELF\x02\x01'+bytes(12)+struct.pack('<H',62))
        value = {'version':1,'platform':'linux-amd64','executor':{'path':executor.name,'sha256':digest(executor)}}
        for name in ('loaders','notices'):
            folder=root/name;folder.mkdir();(folder/'file').write_text('fixture')
            value[name]={'path':name,'files':tree_inventory(folder)}
        manifest=root/'manifest.json';manifest.write_text(json.dumps(value))
        return manifest

    def test_inventory_pins_every_file_and_executor_architecture(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=self.bundle(directory)
            self.assertEqual(set(load(manifest,'linux-amd64')), {'executor','loaders','notices'})
            with self.assertRaisesRegex(ValueError,'architecture'): executor_metadata(Path(directory)/'decklab-flash','linux-arm64')
            (Path(directory)/'notices/extra').write_text('unreviewed')
            with self.assertRaisesRegex(ValueError,'inventory changed'): load(manifest,'linux-amd64')

    def test_checksum_escape_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest=self.bundle(directory)
            binary=Path(directory)/'decklab-flash';binary.write_bytes(binary.read_bytes()+b'tampered')
            with self.assertRaisesRegex(ValueError,'checksum'):load(manifest,'linux-amd64')
            value=json.loads(manifest.read_text());value['executor']['path']='../outside';manifest.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError,'relative'):load(manifest,'linux-amd64')
            try: (Path(directory)/'notices/link').symlink_to(binary)
            except OSError: return  # Windows without CreateSymbolicLink privilege
            with self.assertRaisesRegex(ValueError,'symbolic'):tree_inventory(Path(directory)/'notices')

    def test_archive_rejects_traversal_links_and_unreviewed_payloads(self):
        import io
        import zipfile
        from tools.fetch_install_components import extract
        for name, mode in (('../outside', 0o100644), ('/absolute', 0o100644), ('link', 0o120777)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                archive=io.BytesIO()
                with zipfile.ZipFile(archive,'w') as out:
                    entry=zipfile.ZipInfo(name);entry.external_attr=mode << 16;out.writestr(entry,b'payload')
                archive.seek(0)
                with self.assertRaisesRegex(ValueError,'unsafe'):extract(archive,Path(directory)/'inputs','linux-amd64')
                self.assertFalse((Path(directory)/'inputs').exists())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=self.bundle(root)
            archive=io.BytesIO()
            with zipfile.ZipFile(archive,'w') as out:
                for path in root.rglob('*'):
                    if path.is_file():out.write(path,path.relative_to(root))
            archive.seek(0);extract(archive,root/'accepted','linux-amd64')
            self.assertEqual(set(load(root/'accepted/manifest.json','linux-amd64')),{'executor','loaders','notices'})
