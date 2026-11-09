param([string]$Title, [string]$Message)
$ErrorActionPreference = 'Stop'
# Use the installed host's registered notification identity. An arbitrary title
# is not an AppUserModelID, and no shortcut/registry installation is needed here.
$hostExecutable = [System.IO.Path]::GetFileName((Get-Process -Id $PID).Path)
$hostApp = Get-StartApps | Where-Object {
    [System.IO.Path]::GetFileName($_.AppID) -eq $hostExecutable
} | Select-Object -First 1
if (-not $hostApp) { throw 'No registered notification identity for the PowerShell host' }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$toastXml = New-Object Windows.Data.Xml.Dom.XmlDocument
$safeTitle = [System.Security.SecurityElement]::Escape($Title)
$safeMessage = [System.Security.SecurityElement]::Escape($Message)
$toastXml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$safeTitle</text><text>$safeMessage</text></binding></visual></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($toastXml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($hostApp.AppID).Show($toast)
