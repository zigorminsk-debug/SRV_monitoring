# Run this script from an elevated PowerShell window once the server is configured.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot 'start_windows.ps1'
$TaskName = 'SRV Monitoring Server'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'SRV Monitoring self-hosted server' -Force | Out-Null
Write-Host "Scheduled task '$TaskName' registered. Start it with: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
