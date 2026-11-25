#Requires -Version 5.1
param([string]$Python = 'python', [switch]$PackagingOnly)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    if ($PackagingOnly) {
        $suites = @('test_windows_portable', 'test_windows_entrypoint', 'test_bundle_manifest')
    } else {
        $suites = @('test_request_json', 'test_collector_injection', 'test_serial_log',
                    'test_windows_build_env', 'test_windows_portable', 'test_windows_entrypoint',
                    'test_mcp', 'test_mcp_integration', 'test_mcp_host',
                    'test_mcp_host_proxy', 'test_session_contract', 'test_protocol',
                    'test_transactions', 'test_profiles', 'test_cli',
                    'test_schema', 'test_identity', 'test_identity_association',
                    'test_tasks', 'test_task_actions', 'test_flash_foundation',
                    'test_board_ownership', 'test_agent_revocation', 'test_discrete_task_contract',
                    'test_integration_client', 'test_capabilities', 'test_mcp_leases',
                    'test_flash_runner', 'test_flash_integration', 'test_flash_recovery',
                    'test_flash_targets', 'test_flash_discovery_review', 'test_flash_discovery_integration',
                    'test_pending_labels', 'test_web_settings', 'test_settings_restart',
                    'test_device_preferences', 'test_backend_link', 'test_collector_lock',
                    'test_image_download', 'test_image_catalog', 'test_access_info',
                    'test_install_components', 'test_flash_loaders', 'test_transfer_artifacts',
                    'test_file_tasks', 'test_transfer_tasks', 'test_input_ownership',
                    'test_models', 'test_maintenance', 'test_agent_grants',
                    'test_board_control', 'test_desktop', 'test_desktop_setup',
                    'test_desktop_native', 'test_desktop_provider_stream', 'test_desktop_freerdp',
                    'test_desktop_runtime_package', 'test_websocket_io', 'test_desktop_proxy',
                    'test_desktop_stream', 'test_desktop_stream_wire', 'test_ui_language',
                    'test_boot_compare', 'test_sse', 'test_terminal_backend',
                    'test_terminal_runtime', 'test_console_visibility', 'test_serial_health',
                    'test_demo', 'test_process_windows', 'test_serial_handoff',
                    'test_package_docs', 'test_windows_notices', 'test_bundle_manifest',
                    'test_hardware')
    }
    & $Python tools/run_windows_regressions.py @suites
    if ($LASTEXITCODE -ne 0) { throw 'Windows regression failed.' }
    if (Get-Command node -ErrorAction SilentlyContinue) {
        & node tests/frontend.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Frontend regression tests failed.' }
        & node tests/image-store.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Image store regression tests failed.' }
        & node tests/share.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Agent share UI tests failed.' }
        & node tests/desktop.test.js
        if ($LASTEXITCODE -ne 0) { throw 'Desktop UI tests failed.' }
    } else {
        Write-Warning 'Node.js is unavailable; frontend regression tests were not run.'
    }
} finally { Pop-Location }
