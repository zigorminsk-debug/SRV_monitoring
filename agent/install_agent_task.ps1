# Run in an elevated PowerShell window after agent.json is configured.
$ErrorActionPreference = 'Stop'
$RunScript = Join-Path $PSScriptRoot 'run_agent.ps1'
$TaskName = 'SRV Monitoring Remote Agent'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunScript`"" -WorkingDirectory $PSScriptRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'SRV Monitoring remote Windows agent' -Force | Out-Null
Write-Host "Scheduled task '$TaskName' registered. Start it with: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
