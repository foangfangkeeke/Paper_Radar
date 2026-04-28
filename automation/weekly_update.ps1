param(
    [string]$StartDate = "2023-01-01",
    [string]$EndDate = "",
    [switch]$SkipFetch,
    [switch]$Yes,
    [int]$Limit = 0
)

if ($EndDate -eq "") {
    $EndDate = Get-Date -Format "yyyy-MM-dd"
}

$cmd = @("automation\agent.py", "weekly-update", "--start-date", $StartDate, "--end-date", $EndDate)
if ($SkipFetch) { $cmd += "--skip-fetch" }
if ($Yes) { $cmd += "--yes" }
if ($Limit -gt 0) { $cmd += @("--limit", "$Limit") }

Write-Host "Running: python $($cmd -join ' ')"
python @cmd
