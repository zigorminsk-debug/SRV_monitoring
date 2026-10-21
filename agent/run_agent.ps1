$ErrorActionPreference = 'Stop'
$AgentRoot = $PSScriptRoot
Set-Location $AgentRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    py -3.11 -m venv .venv
}
$Python = Join-Path $AgentRoot '.venv\Scripts\python.exe'
& $Python -c 'import httpx, psutil' *> $null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Agent dependency installation failed' }
}
& $Python (Join-Path $AgentRoot 'monitor_agent.py') --config (Join-Path $AgentRoot 'agent.json')
exit $LASTEXITCODE
