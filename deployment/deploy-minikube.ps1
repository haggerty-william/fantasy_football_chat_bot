#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[1-9][0-9]*$')]
    [string]$LeagueId,

    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Profile = 'minikube',

    [ValidatePattern('^[a-z0-9][a-z0-9-]*$')]
    [string]$Namespace = 'default'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $PSScriptRoot 'k8.yaml'
$secretEnvPath = Join-Path $PSScriptRoot 'secrets.env'
$privateSecretName = 'fantasy-football-private-env'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)."
    }
}

foreach ($tool in @('minikube', 'kubectl')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required command '$tool' was not found on PATH."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'Dockerfile'))) {
    throw "Dockerfile not found in $projectRoot."
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw
$imagePattern = '(?m)^([ \t]*)image:[^\r\n]*'
$leaguePattern = '(?m)(- name: LEAGUE_ID\s*\r?\n[ \t]*value:)[^\r\n]*'
foreach ($pattern in @($imagePattern, $leaguePattern)) {
    if ([regex]::Matches($manifest, $pattern).Count -ne 1) {
        throw 'Expected exactly one image and one LEAGUE_ID value in k8.yaml.'
    }
}
if ($manifest -notmatch '(?m)^  replicas: 1\s*$' -or
    $manifest -notmatch '(?m)^  name: fantasy-football-bot\s*$' -or
    $manifest -notmatch '(?m)^kind: Deployment\s*$') {
    throw 'Expected the single-replica fantasy-football-bot Deployment in k8.yaml.'
}
if ($manifest -match '(?m)^  strategy:') {
    throw 'k8.yaml already defines a strategy. Remove it; this script uses Recreate.'
}

# A fresh tag changes the pod template on every run and prevents stale images.
$image = 'fantasy-football-bot:local-' + [guid]::NewGuid().ToString('N')
$manifest = [regex]::Replace($manifest, $imagePattern, ('${1}image: ' + $image))
$manifest = [regex]::Replace($manifest, $leaguePattern, ('${1} "' + $LeagueId + '"'))
$manifest = [regex]::Replace($manifest, '(?m)^spec:\r?\n', "spec:`n  strategy:`n    type: Recreate`n")
if ($manifest -notmatch '(?m)^[ \t]*imagePullPolicy: Never\s*$') {
    throw 'k8.yaml must use imagePullPolicy: Never for the locally built image.'
}

Write-Host "Starting Minikube profile '$Profile'..."
& minikube start --profile $Profile --keep-context
Assert-NativeSuccess 'Minikube startup'

# Explicit context on every kubectl call; never use the current context implicitly.
& kubectl --context $Profile get namespace $Namespace -o name
Assert-NativeSuccess "Namespace '$Namespace' lookup (create it first if needed)"

if (Test-Path -LiteralPath $secretEnvPath -PathType Leaf) {
    # Only the file path appears in command arguments. Capture errors as well so
    # an invalid env-file line cannot expose a credential in terminal output.
    $savedErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can turn redirected native stderr into terminating
        # errors under Stop. Capture it under Continue and report only safe errors.
        $ErrorActionPreference = 'Continue'
        $secretJson = & kubectl --context $Profile --namespace $Namespace create secret generic $privateSecretName --from-env-file=$secretEnvPath --dry-run=client -o json 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Secret rendering failed.' }
        # Server-side apply avoids storing a duplicate in a last-applied annotation.
        $secretApplyOutput = $secretJson | & kubectl --context $Profile --namespace $Namespace apply --server-side --field-manager=private-env-setup -f - 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Secret apply failed.' }
    } catch {
        throw 'Could not load private configuration. Check deployment/secrets.env KEY=value formatting, cluster access, and Secret permissions without sharing credentials.'
    } finally {
        $ErrorActionPreference = $savedErrorPreference
        $secretJson = $null
        $secretApplyOutput = $null
    }
    Write-Host "Updated private configuration in Kubernetes Secret '$privateSecretName'."
} else {
    $existingSecret = & kubectl --context $Profile --namespace $Namespace get secret $privateSecretName --ignore-not-found -o name
    Assert-NativeSuccess 'Private configuration Secret lookup'
    if ([string]::IsNullOrWhiteSpace(($existingSecret -join ''))) {
        throw 'Private configuration is missing. Copy deployment/secrets.env.example to deployment/secrets.env and fill in the webhook and ESPN cookies before deploying.'
    }
    Write-Host "Using existing Kubernetes Secret '$privateSecretName'."
}

Write-Host "Building $image inside Minikube..."
& minikube image build --profile $Profile --all --tag $image $projectRoot
Assert-NativeSuccess 'Image build'

# The rendered manifest contains Secret references, never credential values.
& kubectl --context $Profile --namespace $Namespace apply -f (Join-Path $PSScriptRoot 'trade-state.yaml')
Assert-NativeSuccess 'Trade state volume apply'
# Recreate stops the previous bot before starting the replacement.
$manifest | & kubectl --context $Profile --namespace $Namespace apply -f -
Assert-NativeSuccess 'Deployment apply'
& kubectl --context $Profile --namespace $Namespace rollout status deployment/fantasy-football-bot --timeout=180s
Assert-NativeSuccess 'Deployment rollout'

Write-Host 'Deployment ready. View logs with:'
Write-Host "kubectl --context $Profile --namespace $Namespace logs deployment/fantasy-football-bot --follow"
