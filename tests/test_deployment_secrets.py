"""Exercise deployment secret handling with every cluster command mocked."""

import base64
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


POWERSHELL = shutil.which('powershell') or shutil.which('pwsh')
DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / 'deployment/deploy-minikube.ps1'


@pytest.mark.skipif(not POWERSHELL, reason='PowerShell is not installed')
@pytest.mark.skipif(not DEPLOY_SCRIPT.is_file(), reason='Deployment files excluded from image')
@pytest.mark.parametrize('scenario', [
    'import', 'existing', 'missing', 'render-failure', 'apply-failure',
])
def test_deployment_secret_handling(tmp_path, scenario):
    deployment = tmp_path / 'deployment'
    deployment.mkdir()
    source = DEPLOY_SCRIPT
    shutil.copyfile(source, deployment / source.name)
    (tmp_path / 'Dockerfile').write_text('FROM scratch\n')
    (deployment / 'trade-state.yaml').write_text('kind: PersistentVolumeClaim\n')
    (deployment / 'k8.yaml').write_text('''apiVersion: apps/v1
kind: Deployment
metadata:
  name: fantasy-football-bot
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: bot-container
          image: fantasy-football-bot:local
          imagePullPolicy: Never
          env:
            - name: LEAGUE_ID
              value: "123"
''')
    if scenario in ('import', 'render-failure', 'apply-failure'):
        (deployment / 'secrets.env').write_text('TEST=PRIVATE_SENTINEL\n')
    script_path = str(deployment / source.name).replace("'", "''")
    python_path = sys.executable.replace("'", "''")
    harness = (f"$scenario = '{scenario}'\n$scriptPath = '{script_path}'\n"
               f"$testPython = '{python_path}'\n") + r'''
$ErrorActionPreference = 'Stop'
$global:deploymentTestBuilt = $false
$global:deploymentTestSecretApplied = $false
function minikube {
    $global:LASTEXITCODE = 0
    if ($args -contains 'build') { $global:deploymentTestBuilt = $true }
}
function kubectl {
    $global:LASTEXITCODE = 0
    if ($args -contains 'generic') {
        if ($scenario -eq 'render-failure') {
            & $testPython -c "import sys; sys.stderr.write('PRIVATE_SENTINEL\n'); sys.exit(3)"
            return
        }
        '{"apiVersion":"v1","kind":"Secret","data":{"secret":"PRIVATE_SENTINEL"}}'
        return
    }
    if ($args -contains '--server-side') {
        $payload = @($input) -join "`n"
        if ($payload -notmatch 'PRIVATE_SENTINEL') { throw 'Secret input was not piped.' }
        if ($args -notcontains '--field-manager=private-env-setup') {
            throw 'Expected secret field manager.'
        }
        if ($scenario -eq 'apply-failure') {
            & $testPython -c "import sys; sys.stderr.write('PRIVATE_SENTINEL\n'); sys.exit(3)"
            return
        }
        $global:deploymentTestSecretApplied = $true
        'secret/fantasy-football-private-env serverside-applied'
        return
    }
    if (($args -contains 'get') -and ($args -contains 'secret')) {
        if ($scenario -eq 'existing') { 'secret/fantasy-football-private-env' }
    }
}
$captured = @()
$caught = $null
try { $captured = & $scriptPath -LeagueId 12345678 *>&1 }
catch { $caught = $_.Exception.Message }
if (($captured -join "`n") -match 'PRIVATE_SENTINEL' -or $caught -match 'PRIVATE_SENTINEL') {
    throw 'Secret appeared in deployment output.'
}
if ($scenario -in @('import','existing')) {
    if ($caught -or -not $global:deploymentTestBuilt) { throw "Expected successful build. $caught" }
    if (($scenario -eq 'import') -and -not $global:deploymentTestSecretApplied) {
        throw 'Secret was not applied.'
    }
} else {
    if (-not $caught -or $global:deploymentTestBuilt) { throw 'Expected failure before build.' }
}
Write-Output "PASS $scenario"
'''
    encoded = base64.b64encode(harness.encode('utf-16-le')).decode('ascii')
    result = subprocess.run(
        [POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'PASS {scenario}' in result.stdout
    assert 'PRIVATE_SENTINEL' not in result.stdout + result.stderr
