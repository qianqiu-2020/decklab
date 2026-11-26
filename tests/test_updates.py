import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from decklab_core import updates


class UpdateTests(unittest.TestCase):
    def release(self, payload=b'debian-package'):
        name = 'decklab_1.20.0_amd64.deb'
        asset = {'name': name, 'state': 'uploaded', 'size': len(payload),
                 'browser_download_url': updates.PROJECT + '/releases/download/v1.20.0/' + name,
                 'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}
        return {'tag_name': 'v1.20.0', 'draft': False, 'prerelease': False, 'assets': [asset]}

    def check(self, data, current='1.19.9'):
        with patch.object(updates, 'open_github', return_value=io.BytesIO(json.dumps(data).encode())):
            return updates.check_update(current=current, target='linux')

    def test_only_exact_stable_release_package_with_a_digest(self):
        release = self.release()
        self.assertTrue(self.check(release)['available'])
        self.assertFalse(self.check(release, current='1.21.0')['available'])
        self.assertIsNotNone(self.check(release)['package'])
        release['assets'][0]['browser_download_url'] = 'https://example.test/decklab.deb'
        self.assertIsNone(self.check(release)['package'])
        with self.assertRaises(updates.UpdateError):
            self.check(dict(release, prerelease=True))

    def test_package_must_match_size_and_digest(self):
        payload = b'debian-package'
        package = self.check(self.release(payload))['package']
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / package['name']
            with patch.object(updates, 'open_github', return_value=io.BytesIO(payload)):
                updates.download_package(package, destination)
            self.assertEqual(destination.read_bytes(), payload)
            with patch.object(updates, 'open_github', return_value=io.BytesIO(b'Z' * len(payload))):
                with self.assertRaises(updates.UpdateError):
                    updates.download_package(package, Path(folder) / 'bad')

    def test_installer_cannot_switch_to_a_different_release(self):
        result = self.check(self.release())
        with patch.object(updates, 'check_update', return_value=result), patch.object(updates, 'install_linux') as install:
            self.assertEqual(updates.main(['1.19.1']), 1)
            install.assert_not_called()
