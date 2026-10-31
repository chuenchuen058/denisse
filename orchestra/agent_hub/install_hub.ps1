# Registers the agent hub as a local MCP server in the Claude desktop app, so the Claude
# chat gets route_task / split_tasks / ask_codex / ask_gemini / ask_copilot / remember.
# Run once, then quit and reopen Claude:
#   powershell -ExecutionPolicy Bypass -File orchestra\agent_hub\install_hub.ps1
# It backs the config up first and only adds or replaces the "agent-hub" entry.
$cfg = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
$server = Join-Path $PSScriptRoot "server.mjs"
$node = (Get-Command node -ErrorAction Stop).Source
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
[IO.File]::WriteAllText($cfg, ($json | ConvertTo-Json -Depth 10))  # UTF-8, no BOM
Write-Host "agent-hub registered in $cfg -> $server"
Write-Host "Quit and reopen Claude. codex-bridge still works; remove it once agent-hub is in use."
