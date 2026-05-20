$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$ReportDir = Join-Path $Root "reports"
$PytestBaseTemp = Join-Path $Root ("data\tmp\pytest-submission-" + [System.Diagnostics.Process]::GetCurrentProcess().Id)
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$VenvCycloneDx = Join-Path $Root ".venv\Scripts\cyclonedx-py.exe"
$CycloneDx = if (Test-Path $VenvCycloneDx) { $VenvCycloneDx } else { "cyclonedx-py" }

New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

function Invoke-Logged {
    param (
        [Parameter(Mandatory = $true)]
        [string] $ReportName,
        [Parameter(Mandatory = $true)]
        [scriptblock] $Command
    )

    $ReportPath = Join-Path $ReportDir $ReportName
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | Tee-Object -FilePath $ReportPath
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        throw "Command failed with exit code $ExitCode. See $ReportPath"
    }
}

Push-Location $Root
try {
    Invoke-Logged "ruff.txt" {
        & $Python -m ruff check api core db infra worker storage tests eval benchmark_parsers.py eval_run.py
    }
    Invoke-Logged "pytest.txt" {
        & $Python -m pytest -p no:cacheprovider --basetemp $PytestBaseTemp tests
    }
    Invoke-Logged "bandit.txt" {
        & $Python -m bandit -r api core db infra worker storage -x tests -ll -f json -o (Join-Path $ReportDir "bandit.json")
    }
    Invoke-Logged "pip-audit.txt" {
        & $Python -m pip_audit -r requirements.txt -r requirements-torch.txt -r requirements-qwen.txt -f json -o (Join-Path $ReportDir "pip-audit.json")
    }
    Invoke-Logged "sbom-runtime.txt" {
        & $CycloneDx requirements --of json -o (Join-Path $ReportDir "sbom-runtime.json") requirements.txt
    }
    Invoke-Logged "sbom-torch.txt" {
        & $CycloneDx requirements --of json -o (Join-Path $ReportDir "sbom-torch.json") requirements-torch.txt
    }
    Invoke-Logged "sbom-qwen.txt" {
        & $CycloneDx requirements --of json -o (Join-Path $ReportDir "sbom-qwen.json") requirements-qwen.txt
    }
    Invoke-Logged "compose.txt" { docker compose -f docker-compose.yml config --quiet }
    Invoke-Logged "compose-onprem.txt" { docker compose -f docker-compose.onprem.yml config --quiet }
}
finally {
    Pop-Location
}
