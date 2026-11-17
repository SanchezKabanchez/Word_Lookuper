param(
    [Parameter(Mandatory = $true)][string]$ProcessName,
    [string]$PresentMonPath = 'PresentMon.exe',
    [ValidateRange(5, 180)][int]$Seconds = 30,
    [ValidatePattern('^[A-Za-z0-9_-]{1,40}$')][string]$Label = 'baseline',
    [string]$OutputDirectory = (Join-Path (Get-Location) 'fps-captures')
)
$ErrorActionPreference = 'Stop'
$binary = Get-Command $PresentMonPath -CommandType Application -ErrorAction SilentlyContinue
if (-not $binary) {
    throw 'Install the official PresentMon console application and pass -PresentMonPath. https://github.com/GameTechDev/PresentMon/releases'
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
$csv = Join-Path (Resolve-Path $OutputDirectory) "$stamp-$Label.csv"
$started = [DateTime]::UtcNow.ToString('o')
# v1 metrics make the frame-time column unambiguous for the summary script.
& $binary.Source --process_name $ProcessName --timed $Seconds --terminate_after_timed --v1_metrics --output_file $csv
if ($LASTEXITCODE -ne 0) {
    throw "PresentMon failed ($LASTEXITCODE). Check its message and Performance Log Users permissions."
}
@{ label = $Label; process_name = $ProcessName; start_utc = $started;
   end_utc = [DateTime]::UtcNow.ToString('o'); seconds = $Seconds } |
    ConvertTo-Json | Set-Content -Encoding UTF8 "$csv.metadata.json"
Write-Output "Saved $csv"
