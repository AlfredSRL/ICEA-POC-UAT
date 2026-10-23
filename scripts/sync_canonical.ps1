param(
    [Parameter(Mandatory = $true)]
    [string] $ServerRepo,
    [ValidatePattern('^[0-9a-f]{7,40}$')]
    [string] $Commit
)
# Refresh the vendored canonical modules from a server commit (default: HEAD)
# and update MANIFEST.json. Copies source code only: never data/dev or runtime.

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$target = Join-Path $repoRoot 'src/icea_client/canonical'
$server = (Resolve-Path -LiteralPath $ServerRepo).Path
if (-not $Commit) { $Commit = 'HEAD' }
$resolved = (git -C $server rev-parse --verify "$Commit^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { throw "Unknown server commit: $Commit" }
$files = @('contracts.py', 'schemas.py', 'normalization.py', 'evaluation.py',
           'ground_truth.py', 'metrics.py', 'export_field_comparison.py')
$manifestPath = Join-Path $target 'MANIFEST.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
$hashes = [ordered]@{}
foreach ($file in $files) {
    $destination = Join-Path $target $file
    # cmd redirection keeps the committed bytes unchanged (no re-encoding).
    cmd /c "git -C `"$server`" show $($resolved):src/$file > `"$destination`""
    if ($LASTEXITCODE -ne 0) { throw "Cannot read src/$file at $resolved" }
    $hashes[$file] = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest.server_commit = $resolved
$manifest.server_working_tree = 'copied from the commit (git show), not from the working tree'
$manifest.copied_at = (Get-Date -Format 'yyyy-MM-dd')
$manifest.files = [pscustomobject] $hashes
$json = $manifest | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($manifestPath, $json + "`n", [System.Text.UTF8Encoding]::new($false))
$hashes.GetEnumerator() | ForEach-Object { "$($_.Value)  $($_.Key)" }
git -C $repoRoot diff --stat -- src/icea_client/canonical
Write-Host "Canonical modules refreshed from $resolved. Run: .\scripts\uat.ps1 -Command test"
