# Registers the agent hub as a local MCP server in the Claude desktop app, so the Claude
# chat gets route_task / split_tasks / ask_codex / ask_gemini / ask_copilot / remember.
# Run once, then quit Claude fully (tray icon -> Quit) and reopen it:
#   powershell -ExecutionPolicy Bypass -File orchestra\agent_hub\install_hub.ps1
# The desktop app (Cowork) reads %LOCALAPPDATA%\Claude-Data\<id>\claude_desktop_config.json,
# not %APPDATA%\Claude\ (found 2 Nov 2026: the hub was registered there and never loaded).
# Each config is backed up first; only the "agent-hub" entry is added or replaced.
$server = Join-Path $PSScriptRoot "server.mjs"
$node = (Get-Command node -ErrorAction Stop).Source
$configs = @(Get-ChildItem (Join-Path $env:LOCALAPPDATA "Claude-Data") -Recurse -Depth 1 -Filter claude_desktop_config.json -ErrorAction SilentlyContinue | ForEach-Object FullName)
if (-not $configs) { $configs = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json") }
foreach ($cfg in $configs) {
    if (Test-Path $cfg) {
        Copy-Item $cfg "$cfg.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
        $json = Get-Content $cfg -Raw | ConvertFrom-Json
    } else {
        New-Item -ItemType Directory -Force (Split-Path $cfg) | Out-Null
        $json = [pscustomobject]@{}
    }
    if (-not $json.PSObject.Properties["mcpServers"]) {
        $json | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    $entry = [pscustomobject]@{ command = $node; args = @($server) }
    $json.mcpServers | Add-Member -NotePropertyName "agent-hub" -NotePropertyValue $entry -Force
    [IO.File]::WriteAllText($cfg, ($json | ConvertTo-Json -Depth 20))  # UTF-8, no BOM
    Write-Host "agent-hub registered in $cfg"
}
Write-Host "Quit Claude fully (tray icon -> Quit), then reopen it."
