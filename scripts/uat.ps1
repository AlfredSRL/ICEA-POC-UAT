param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('test', 'build', 'preflight', 'run', 'compare', 'all', 'check-api')]
    [string] $Command,
    [string] $GroundTruth,
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string[]] $Samples,
    [ValidateSet('A', 'B', 'C', 'D')]
    [string[]] $Methods,
    [string] $Layout,
    [switch] $Execute,
    [int] $ApproveCalls = -1,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location $repoRoot
try {
    if ($Command -eq 'build') {
        docker compose build client
        exit $LASTEXITCODE
    }
    if ($Command -eq 'test') {
        docker compose run --rm tests
        exit $LASTEXITCODE
    }
    if ($Command -ne 'check-api' -and -not $GroundTruth) {
        throw '-GroundTruth is required (file name inside data/uat/ground_truth).'
    }
    if ($Command -in @('run', 'all') -and -not $Samples) {
        throw '-Samples is required: there is no mass-run option.'
    }
    if (($Execute -or $Command -eq 'check-api') -and -not (Test-Path -LiteralPath (Join-Path $repoRoot '.env'))) {
        throw 'Local .env is missing; copy .env.example and set ICEA_API_URL and ICEA_API_TOKEN.'
    }
    # The shared network must exist before compose attaches the client to it.
    $network = 'icea-api'
    $envFile = Join-Path $repoRoot '.env'
    if (Test-Path -LiteralPath $envFile) {
        $setting = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*ICEA_API_DOCKER_NETWORK\s*=\s*\S' } |
            Select-Object -First 1
        if ($setting) { $network = ($setting -split '=', 2)[1].Trim() }
    }
    # Windows PowerShell turns native stderr into an error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    docker network inspect $network *> $null
    $found = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if (-not $found) {
        docker network create $network | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot create Docker network $network" }
    }
    $arguments = @($Command)
    if ($GroundTruth) {
        # Inside the container the GT directory is mounted at the default location.
        $arguments += @('--ground-truth', (Split-Path -Leaf $GroundTruth))
    }
    if ($Layout) {
        # A custom layout lives beside the workbook in data/uat/ground_truth.
        $arguments += @('--layout', ('/app/data/uat/ground_truth/' + (Split-Path -Leaf $Layout)))
    }
    if ($Samples) { $arguments += @('--samples') + $Samples }
    if ($Methods) { $arguments += @('--methods') + $Methods }
    if ($Execute) {
        $arguments += '--execute'
        if ($ApproveCalls -lt 0) { throw '-Execute requires -ApproveCalls equal to the planned budget.' }
        $arguments += @('--approve-calls', [string] $ApproveCalls)
    }
    if ($Force) { $arguments += '--force' }
    # Native stderr (warnings, unittest progress) must not abort under 'Stop'.
    $ErrorActionPreference = 'Continue'
    docker compose run --rm client @arguments
    exit $LASTEXITCODE
}
finally { Pop-Location }
