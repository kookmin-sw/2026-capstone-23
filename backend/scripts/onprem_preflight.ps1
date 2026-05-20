param(
    [string]$EnvFile,
    [switch]$SkipDocker,
    [switch]$SkipGpu,
    [switch]$SkipComposeConfig,
    [switch]$SkipPortCheck
)

$ErrorActionPreference = "Stop"
$BackendRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $EnvFile) {
    $EnvFile = Join-Path $BackendRoot ".env"
}

$errors = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Add-CheckError([string]$Message) {
    $errors.Add($Message) | Out-Null
}

function Add-CheckWarning([string]$Message) {
    $warnings.Add($Message) | Out-Null
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $index = $trimmed.IndexOf("=")
        if ($index -le 0) {
            continue
        }
        $key = $trimmed.Substring(0, $index).Trim()
        $value = $trimmed.Substring($index + 1).Trim()
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$key] = $value
    }
    return $values
}

$dotenv = Read-DotEnv $EnvFile

function Get-ConfigValue([string]$Name, [string]$Default = "") {
    $envValue = [Environment]::GetEnvironmentVariable($Name)
    if ($null -ne $envValue -and $envValue.Trim() -ne "") {
        return $envValue.Trim()
    }
    if ($dotenv.ContainsKey($Name) -and [string]$dotenv[$Name] -ne "") {
        return ([string]$dotenv[$Name]).Trim()
    }
    return $Default
}

function Test-Placeholder([string]$Value) {
    $normalized = $Value.Trim().ToLowerInvariant()
    if ($normalized -eq "") {
        return $true
    }
    $placeholders = @(
        "admin",
        "password",
        "changeme",
        "change-me",
        "change-me-before-use",
        "change-me-admin-ui-secret",
        "change-me-document-encryption-key",
        "luminir-local-password",
        "replace-with-secret",
        "replace_with_secret"
    )
    return $placeholders.Contains($normalized) -or $normalized.StartsWith("change-me")
}

function Require-Secret([string]$Name, [int]$MinLength) {
    $value = Get-ConfigValue $Name
    if (Test-Placeholder $value) {
        Add-CheckError "$Name must be set and must not use a placeholder/default value."
        return
    }
    if ($value.Length -lt $MinLength) {
        Add-CheckError "$Name must be at least $MinLength characters."
    }
}

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Add-CheckError ".env file was not found: $EnvFile"
}

$adminId = Get-ConfigValue "ADMIN_ID"
if (Test-Placeholder $adminId) {
    Add-CheckError "ADMIN_ID must be set to a non-placeholder administrator id."
}
Require-Secret "ADMIN_PW" 12
Require-Secret "ADMIN_UI_SECRET_KEY" 32
Require-Secret "APP_SECRET_KEY" 32
Require-Secret "RABBITMQ_PASSWORD" 16

if ((Get-ConfigValue "AUTH_DISABLED" "0").ToLowerInvariant() -in @("1", "true", "yes", "on")) {
    Add-CheckError "AUTH_DISABLED must be false for on-prem deployment."
}
if ((Get-ConfigValue "AUTH_REQUIRED" "1").ToLowerInvariant() -in @("0", "false", "no", "off")) {
    Add-CheckError "AUTH_REQUIRED must be true for on-prem deployment."
}
if ((Get-ConfigValue "QUEUE_BACKEND" "rabbitmq").ToLowerInvariant() -ne "rabbitmq") {
    Add-CheckError "QUEUE_BACKEND must be rabbitmq for on-prem distributed workers."
}
if ((Get-ConfigValue "QUEUE_MEMORY_FALLBACK_ENABLED" "0").ToLowerInvariant() -in @("1", "true", "yes", "on")) {
    Add-CheckError "QUEUE_MEMORY_FALLBACK_ENABLED must be false for on-prem deployment."
}
if ((Get-ConfigValue "STORE_BACKEND" "sqlite").ToLowerInvariant() -ne "sqlite") {
    Add-CheckWarning "STORE_BACKEND is not sqlite. This check keeps SQLite as the expected on-prem default."
}

$modelHostDirRaw = Get-ConfigValue "QWEN_MODEL_HOST_DIR" "../models"
$modelHostDir = [System.IO.Path]::GetFullPath((Join-Path $BackendRoot $modelHostDirRaw))
if ([System.IO.Path]::IsPathRooted($modelHostDirRaw)) {
    $modelHostDir = [System.IO.Path]::GetFullPath($modelHostDirRaw)
}

$containerModelPath = Get-ConfigValue "QWEN_VL_7B_MODEL_PATH" "/models/Qwen2.5-VL-7B-Instruct"
$modelRelative = "Qwen2.5-VL-7B-Instruct"
if ($containerModelPath.StartsWith("/models/")) {
    $modelRelative = $containerModelPath.Substring("/models/".Length)
}
$modelPath = Join-Path $modelHostDir $modelRelative
if (-not (Test-Path -LiteralPath $modelPath -PathType Container)) {
    Add-CheckError "Qwen model directory was not found: $modelPath"
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $modelPath "config.json") -PathType Leaf)) {
        Add-CheckError "Qwen model config.json was not found: $modelPath"
    }
    $weights = Get-ChildItem -LiteralPath $modelPath -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "*.safetensors" -or $_.Name -like "*.bin" -or $_.Name -like "*.safetensors.index.json" }
    if (-not $weights) {
        Add-CheckError "Qwen model weight files were not found: $modelPath"
    }
}

$minFreeDiskGb = [int](Get-ConfigValue "ONPREM_MIN_FREE_DISK_GB" "20")
$root = [System.IO.Path]::GetPathRoot($BackendRoot)
$driveName = $root.Substring(0, 1)
$drive = Get-PSDrive -Name $driveName -ErrorAction SilentlyContinue
if ($minFreeDiskGb -le 0) {
    Add-CheckWarning "Disk free-space threshold is disabled because ONPREM_MIN_FREE_DISK_GB is $minFreeDiskGb."
} elseif ($drive) {
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($drive.Free -lt ($minFreeDiskGb * 1GB)) {
        Add-CheckError "Free disk space on ${driveName}: is ${freeGb}GB; required at least ${minFreeDiskGb}GB."
    }
} else {
    Add-CheckWarning "Could not inspect free disk space for $BackendRoot."
}

if (-not $SkipPortCheck) {
    $ports = @(
        [int](Get-ConfigValue "BACKEND_PORT" "8000"),
        [int](Get-ConfigValue "REDIS_PORT" "6379"),
        [int](Get-ConfigValue "RABBITMQ_PORT" "5672"),
        [int](Get-ConfigValue "RABBITMQ_MANAGEMENT_PORT" "15672")
    )
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    foreach ($port in $ports) {
        if ($listeners | Where-Object { $_.Port -eq $port }) {
            Add-CheckError "TCP port $port is already in use."
        }
    }
}

if (-not $SkipDocker) {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Add-CheckError "docker command was not found."
    } else {
        & docker version *> $null
        if ($LASTEXITCODE -ne 0) {
            Add-CheckError "docker daemon is not reachable."
        }
        & docker compose version *> $null
        if ($LASTEXITCODE -ne 0) {
            Add-CheckError "docker compose plugin is not available."
        }
        if (-not $SkipComposeConfig) {
            Push-Location $BackendRoot
            try {
                & docker compose -f docker-compose.onprem.yml config --quiet
                if ($LASTEXITCODE -ne 0) {
                    Add-CheckError "docker-compose.onprem.yml config validation failed."
                }
            } finally {
                Pop-Location
            }
        }
    }
}

if (-not $SkipGpu) {
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) {
        Add-CheckError "nvidia-smi was not found. Install NVIDIA driver and container toolkit before running Qwen GPU workers."
    } else {
        & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
        if ($LASTEXITCODE -ne 0) {
            Add-CheckError "nvidia-smi failed."
        }
    }
}

foreach ($warning in $warnings) {
    Write-Warning $warning
}

if ($errors.Count -gt 0) {
    Write-Host "On-prem preflight failed:" -ForegroundColor Red
    foreach ($item in $errors) {
        Write-Host " - $item" -ForegroundColor Red
    }
    exit 1
}

Write-Host "On-prem preflight passed." -ForegroundColor Green
exit 0
