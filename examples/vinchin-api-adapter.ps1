# Generic, read-only adapter for an endpoint documented by your Vinchin version.
# Copy vinchin-api.json.example to a local file, edit its URL/JSON paths, then restrict
# the file ACL: it contains a read-only API credential. This intentionally does not
# guess an undocumented Vinchin URL or schema.
param(
    [string]$ConfigPath = 'C:\ProgramData\SRV-Monitoring\vinchin-api.json'
)
$ErrorActionPreference = 'Stop'

function Get-JsonPathValue($Object, [string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return ,$Object }
    $Value = $Object
    foreach ($Part in $Path.Trim('.').Split('.')) {
        if ($null -eq $Value) { return $null }
        if ($Value -is [System.Collections.IDictionary]) {
            $Value = $Value[$Part]
        } elseif ($Value -is [System.Array] -and $Part -match '^\d+$') {
            $Index = [int]$Part
            if ($Index -ge $Value.Count) { return $null }
            $Value = $Value[$Index]
        } else {
            $Property = $Value.PSObject.Properties[$Part]
            if ($null -eq $Property) { return $null }
            $Value = $Property.Value
        }
    }
    return ,$Value
}

try {
    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Config.url -notmatch '^https://') { throw 'Use an HTTPS endpoint with a valid certificate.' }
    $Headers = @{ Accept = 'application/json' }
    switch ([string]$Config.auth_type) {
        'basic' {
            $Raw = [Text.Encoding]::UTF8.GetBytes("$($Config.username):$($Config.password)")
            $Headers.Authorization = 'Basic ' + [Convert]::ToBase64String($Raw)
        }
        'bearer' { $Headers.Authorization = 'Bearer ' + [string]$Config.bearer_token }
        'none' { }
        default { throw 'auth_type must be basic, bearer, or none.' }
    }
    $Response = Invoke-RestMethod -Uri ([string]$Config.url) -Method Get -Headers $Headers -TimeoutSec 20
    $Jobs = Get-JsonPathValue $Response ([string]$Config.jobs_path)
    if ($null -eq $Jobs) { throw "No jobs found at JSON path '$($Config.jobs_path)'" }
    if ($Jobs -isnot [System.Array]) { $Jobs = @($Jobs) }
    $Success = @($Config.success_values | ForEach-Object { ([string]$_).ToLowerInvariant() })
    $Failure = @($Config.failure_values | ForEach-Object { ([string]$_).ToLowerInvariant() })
    $Failed = 0
    $Warnings = 0
    $LastSuccess = $null
    $Now = [DateTimeOffset]::UtcNow
    foreach ($Job in $Jobs) {
        $JobStatus = [string](Get-JsonPathValue $Job ([string]$Config.status_path))
        $JobName = [string](Get-JsonPathValue $Job ([string]$Config.job_name_path))
        $LastRun = Get-JsonPathValue $Job ([string]$Config.last_run_path)
        $State = $JobStatus.ToLowerInvariant()
        if ($Failure -contains $State) { $Failed++ }
        elseif ($Success -notcontains $State -and $State -notin @('running','in progress','started','processing')) { $Warnings++ }
        if ($Success -contains $State -and $LastRun) {
            try {
                $Parsed = [DateTimeOffset]::Parse([string]$LastRun).ToUniversalTime()
                if ($null -eq $LastSuccess -or $Parsed -gt $LastSuccess) { $LastSuccess = $Parsed }
                if ([int]$Config.expected_interval_minutes -gt 0 -and ($Now - $Parsed).TotalMinutes -gt [int]$Config.expected_interval_minutes) { $Warnings++ }
            } catch { $Warnings++ }
        }
    }
    if ($Jobs.Count -eq 0) { $Warnings++ }
    $Overall = if ($Failed -gt 0) { 'critical' } elseif ($Warnings -gt 0) { 'warning' } else { 'ok' }
    $Message = "Jobs=$($Jobs.Count); failed=$Failed; warnings=$Warnings"
    if ($LastSuccess) { $Message += "; latest_success=$($LastSuccess.ToString('o'))" }
    [ordered]@{ status = $Overall; name = 'Vinchin Backup'; message = $Message } | ConvertTo-Json -Compress
} catch {
    [ordered]@{ status = 'warning'; name = 'Vinchin Backup API'; message = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 2
}
