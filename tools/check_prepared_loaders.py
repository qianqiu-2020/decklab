#!/usr/bin/env python3
"""Exercise real Go image import and Python validation with USB phase mocked.

This performs filesystem-only `image-plan`; no discovery, check-plan, preparation
or execute operation reaches the external binary or physical hardware.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seriald_core.flash_runner import BoardControl, FlashBinding, LocalFlashExecutor, SerialEndpoint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executor', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='decklab-ram-plan-') as directory:
        root = Path(directory)
        payload = root / 'opensbi.itb'
        payload.write_bytes(b'offline single partition payload')
        sha = hashlib.sha256(payload.read_bytes()).hexdigest()
        profile = {'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3',
            'bootrom_identity': {'version-IC': 'SPA3609', 'version-brom': '1.0'},
            'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt', 'sector_size': 512,
                          'size_getvar': '__decklab_gpt_capacity', 'partitions': None, 'sparse': False}]}
        binding = FlashBinding('', 1, profile['id'], profile, BoardControl(), SerialEndpoint(provider='none'),
            'offline-fixture', (str(args.executor.resolve()),),
            {'port_path': [99, 99], 'vendor_id': 0x361c, 'usb_address': 99}, target_id='offline-target',
            target_generation='offline-generation')
        executor = LocalFlashExecutor(root / 'flashing')
        original = executor._run
        operations = []
        def run(current, operation, request, *values, **keywords):
            operations.append(operation)
            if operation == 'check-plan':
                return {'plan': request['plan'], 'payload_candidates': request['payload_candidates'], 'requires_prepare': True}
            if operation != 'image-plan':
                raise AssertionError('hardware operation forbidden in offline validation')
            return original(current, operation, request, *values, **keywords)
        with mock.patch.dict(os.environ, {'SERIALD_FLASH_LOADERS': str(args.manifest.resolve())}), \
                mock.patch.object(executor, '_run', side_effect=run):
            result = executor.plan(binding, {'id': 'offline-image', 'path': str(payload), 'name': payload.name,
                'size': payload.stat().st_size, 'sha256': sha}, 'partitions')
        plan = result['plan']
        assert plan['write_layout'] is False and plan['writes'] == []
        assert len(plan['ram_loaders']) == 2
        assert len(result['execution_profile']['prepare']) == 4
        assert result['execution_profile']['ram_loader_protocol'] == 'decklab-nor-v1'
        assert operations == ['image-plan', 'check-plan']
        print(json.dumps({'ok': True, 'external_operations': ['image-plan'], 'usb_phase': 'mocked',
            'storage_writes': [], 'ram_loaders': plan['ram_loaders'], 'payload_sha256': sha}, indent=2))


if __name__ == '__main__':
    main()
