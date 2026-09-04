# Denisse background eval harness.
#
# Launched from the Startup folder, stays resident in a visible window,
# runs the prompt suite once now and then once a day at RunHour. Each run
# writes a dated report into the vault (Denisse\Evals\) and appends any
# newly-verified good exchanges to eval\dataset.jsonl.
#
# Visible on purpose -- Chuen asked to be able to watch it. The cost is
# that closing the window stops it until next login; the Startup shortcut
# is what brings it back.

param(
    [int]$RunHour = 3,          # local hour for the daily run
    [switch]$Once               # run the suite once and exit
)

$ErrorActionPreference = 'Continue'
$Repo   = 'C:\Users\Xichu\GitHub\denisse'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Log    = Join-Path $Repo 'eval\harness.log'

function Write-Line($msg, $color = 'Gray') {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line -ForegroundColor $color
    Add-Content -Path $Log -Value $line -Encoding utf8
}

function Invoke-Suite {
    Write-Line 'running eval suite...' 'Cyan'
    $out = & $Python (Join-Path $Repo 'eval\run_eval.py') --quiet 2>&1
    $code = $LASTEXITCODE
    foreach ($l in $out) {
        if ("$l".Trim()) { Write-Line "  $l" $(if ($code -eq 0) { 'Green' } else { 'Yellow' }) }
    }
    if ($code -eq 0) {
        Write-Line 'all live cases passed' 'Green'
    } else {
        Write-Line "exit $code -- some cases failed, see the report" 'Yellow'
    }
}

$host.UI.RawUI.WindowTitle = 'Denisse eval harness'
Write-Host ''
Write-Host '  Denisse background eval harness' -ForegroundColor Magenta
Write-Host '  reports -> vault Denisse\Evals\   dataset -> eval\dataset.jsonl' -ForegroundColor DarkGray
Write-Host '  Ctrl+C to stop. Closing this window stops it until next login.' -ForegroundColor DarkGray
Write-Host ''

if (-not (Test-Path $Python)) {
    Write-Line "FATAL: no python at $Python" 'Red'
    Start-Sleep -Seconds 30
    exit 1
}

Invoke-Suite
if ($Once) { exit }

while ($true) {
    # Next occurrence of RunHour, local time.
    $now  = Get-Date
    $next = (Get-Date -Hour $RunHour -Minute 0 -Second 0)
    if ($next -le $now) { $next = $next.AddDays(1) }
    $wait = [int]($next - $now).TotalSeconds

    Write-Line ("next run {0} (in {1:N1}h)" -f $next.ToString('yyyy-MM-dd HH:mm'), ($wait / 3600))

    # Sleep in chunks so the window stays responsive to Ctrl+C and so a
    # laptop that slept through the target hour still catches up promptly
    # rather than waiting out a stale single long sleep.
    while ((Get-Date) -lt $next) {
        Start-Sleep -Seconds ([Math]::Min(300, [Math]::Max(5, [int](($next - (Get-Date)).TotalSeconds))))
    }

    Invoke-Suite
}
