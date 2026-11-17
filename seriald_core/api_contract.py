"""Static public protocol contract, independent of devices and credentials.

This describes implemented interfaces. Per-target capability queries and the
request's authorization still decide whether an operation may run.
"""

API_VERSION = 1
CONTRACT_NAME = 'decklab-execution'
CONTRACT_REVISION = 1

OUTCOMES = ('not-issued', 'issued', 'acknowledged', 'observed', 'verified', 'unknown')
BASE_TASK_KINDS = ('identify', 'ssh-enable', 'ssh-connect', 'file-copy',
                   'file-upload', 'file-download', 'flash', 'nor-backup')


def execution_capabilities(version, hardware_actions, hardware_adapter):
    """Return fresh JSON-compatible data; never inspect an active device."""
    submitted = list(BASE_TASK_KINDS) + ['hardware-' + action for action in hardware_actions]
    produced = [kind for kind in submitted if kind != 'nor-backup'] + [
        'flash-inspect', 'desktop-input', 'image-download']
    return {
        'api_version': API_VERSION, 'version': version,
        'contract': {
            'name': CONTRACT_NAME, 'revision': CONTRACT_REVISION,
            'compatibility': {
                'unknown_response_fields': 'ignore',
                'missing_capabilities': 'unsupported',
                'unknown_outcomes': 'unknown',
                'breaking_changes': 'api-version',
            },
        },
        'scope': {
            'discovery': 'protocol', 'authorization': 'not-a-grant',
            'availability': 'per-target', 'execution': 'single-collector',
            'execution_location': 'collector-host',
            'hardware_location': 'configured-local-or-remote-endpoints',
            'cross_platform_exclusive': False, 'remote_executor_nodes': False,
            'fleet_scheduling': False, 'high_availability': False,
        },
        'execution_lease': {
            'supported': True, 'scope': 'collector', 'resource': 'board', 'optional': True,
            'claim_endpoint': '/api/input-claim', 'claim_scope': 'board',
            'renew_endpoint': '/api/input-renew', 'release_endpoint': '/api/input-release',
            'query_endpoint': '/api/input-owner', 'ttl_seconds': {'min': 30, 'max': 3600},
            'proof_fields': ['id', 'generation', 'token'], 'generation_type': 'integer',
            'cross_platform_exclusive': False,
            'expiry': 'recovery', 'restart': 'recovery',
            'claim_replay_supported': False, 'proof_storage': 'hash-only',
            'recovery': {'endpoint': '/api/input-release', 'human_owner_only': True,
                         'requires_expected_generation': True, 'requires_stop_confirmation': True},
        },
        'features': {
            'serial': True, 'ssh': True, 'sse_offsets': True, 'shared_terminal': True,
            'boot_archives': True, 'boot_compare': True,
            'custom_probe_commands': True, 'unidentified_terminal_access': True,
            'operation_status': True, 'operation_cancel': True, 'durable_tasks': True,
            'desktop': True, 'flashing': True, 'nor_workspace': True,
            'image_catalog': True, 'managed_artifacts': True,
        },
        'tasks': {
            # Keep kinds as the accepted /api/task-start values for old clients.
            'kinds': submitted, 'produced_kinds': produced, 'restart': 'interrupt',
            'automatic_replay': False, 'file_transfer': ['copy', 'upload', 'download'],
            'submission': {
                '/api/task-start': submitted,
                '/api/desktop/input': ['desktop-input'],
                '/api/image-download': ['image-download'],
                '/api/image-prepare': ['image-prepare'],
            },
            'kind_aliases': {'nor-backup': 'flash'},
            'request_dedup': {
                'supported': True, 'parameter': 'request_id',
                'scope': ['actor', 'request_id'], 'persistent': True,
                'kinds': submitted + ['desktop-input', 'image-download', 'image-prepare'],
                'required_kinds': ['flash', 'nor-backup', 'desktop-input', 'image-download'],
                'format': '[A-Za-z0-9_-]{8,100}',
                'same_input': 'return-existing-task', 'changed_input_http_status': 409,
            },
            'request_lookup': {
                'supported': True, 'endpoint': '/api/task',
                'parameter': 'request_id', 'scope': 'actor',
            },
            'outcome': {
                'field': 'outcome', 'values': list(OUTCOMES),
                'independent_of_state': True, 'completed_is_verified': False,
                'unknown_requires_observation': True,
            },
            'cancel': {
                'endpoint': '/api/task-cancel', 'cooperative': True,
                'request_is_stop_confirmation': False, 'rollback': False,
            },
            'cleanup_recovery': {'endpoint': '/api/task-recovery', 'human_owner_only': True,
                                 'requires_expected_updated': True, 'preserves_original_outcome': True},
        },
        'authorization': {
            'grants': ['all', 'device', 'connection'],
            'revocation': {'endpoint': '/api/agent-revoke', 'durable': True,
                           'generation_field': 'revocation_generation',
                           'confirmation_field': 'sessions_closed', 'pending_field': 'revocation_pending',
                           'network_partition_is_stop_confirmation': False},
        },
        'artifacts': {
            'chunk_size': 1024 ** 2, 'append_body_limit': 2 * 1024 ** 2,
            'download': 'stream', 'integrity': 'sha256',
            'actions': ['create', 'append', 'seal', 'delete', 'download'],
        },
        'file_transfer': {
            'transport': 'serial', 'requires_linux_shell': True, 'integrity': 'sha256',
        },
        'ssh_provisioning': 'profile-controlled', 'input_ownership': 'optional-client',
        'hardware': {
            'actions': list(hardware_actions), 'configuration': 'explicit-serial',
            'host_adapter': hardware_adapter, 'default': 'disabled',
            'availability_endpoint': '/api/hardware',
        },
        'desktop': {
            'availability_endpoint': '/api/desktop/sessions',
            'discovery_endpoint': '/api/desktop/discover', 'discovery_scope': 'known-board-ips-and-fixed-ports',
            'configuration_endpoint': '/api/desktop/configure', 'configuration_owner_only': True,
            'configuration_storage': 'existing-web-settings-desktop-document',
            'providers': ['helper', 'vnc', 'rdp'], 'capture': 'png',
            'input_actions': ['click', 'scroll', 'key', 'text'],
            'requires_identified_board': True, 'requires_configuration': True,
            'fresh_frame_required': True, 'continuous_browser_stream': True,
            'stream_endpoint': '/api/desktop/stream', 'stream_subprotocol': 'decklab-desktop-v1',
            'stream_providers': ['vnc', 'rdp'], 'server_credentials_only': True,
            'stream_input': ['pointer', 'key', 'wheel', 'text', 'clipboard', 'clipboard-read', 'release_all'],
            'cross_platform_exclusive': False,
        },
        'flashing': {
            'configuration': 'automatic-usb-or-explicit-board-binding',
            'discovery': 'local-usb', 'requires_executor': True, 'automatic_actor': 'web',
            'reset': 'manual', 'task_target': 'board-or-usb-target',
            'reset_after': 'explicit-preview-option',
            'availability_endpoint': '/api/flash-capabilities',
            'discovery_endpoint': '/api/flash-discover', 'probe_endpoint': '/api/flash-target',
            'plan_endpoint': '/api/flash-plan', 'selection_endpoint': '/api/flash-selection',
            'events_endpoint': '/api/flash-events', 'recovery_endpoint': '/api/flash-recovery',
            'modes': ['full', 'partitions'], 'verification': ['required', 'if-supported', 'none'],
            'readback': 'per-target',
            'nor_backup': {
                'submit_kind': 'nor-backup', 'task_kind': 'flash',
                'availability_field': 'nor_workspace_supported',
                'requires_supported_k3_usb_target': True,
            },
            'storage_inventory': {'availability_field': 'storage_inventory_supported'},
        },
        'images': {
            'catalog_endpoint': '/api/image-catalog', 'library_endpoint': '/api/image-library',
            'import_endpoint': '/api/image-download', 'import_task_kind': 'image-download',
            'prepare_endpoint': '/api/image-prepare', 'prepare_task_kind': 'image-prepare',
            'preparation_requires_device': False,
        },
    }
