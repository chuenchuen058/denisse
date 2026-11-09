# One Windows toast from Orchestra (orchestra/visible.py): powershell -File toast.ps1 -Text "..."
# The text goes in as a text node, never as XML, so a task title can't break the toast.
# AppId: Windows PowerShell's own registered app id (an OS constant), so no shortcut or
# registration is needed for the toast to show.
param(
    [Parameter(Mandatory = $true)][string]$Text,
    [string]$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
)
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText01)
$xml.GetElementsByTagName('text').Item(0).AppendChild($xml.CreateTextNode($Text)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
