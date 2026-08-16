param(
    [string]$OutputDirectory = "backups",
    [string]$EnvFile = ".env.docker"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFilePath = Join-Path $projectRoot $EnvFile

if (-not (Test-Path -LiteralPath $envFilePath)) {
    throw "Arquivo de ambiente não encontrado: $envFilePath"
}
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExecutable = $dockerCommand.Source
} else {
    $dockerCandidates = @(
        (Join-Path ${env:ProgramFiles} "Docker\Docker\resources\bin\docker.exe"),
        (Join-Path ${env:LOCALAPPDATA} "Programs\DockerDesktop\resources\bin\docker.exe")
    )
    $dockerExecutable = $dockerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not (Test-Path -LiteralPath $dockerExecutable)) {
    throw "Docker CLI não encontrado no PATH nem no local padrão do Docker Desktop."
}

$settings = @{}
Get-Content -LiteralPath $envFilePath | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $name, $value = $line -split "=", 2
        $settings[$name.Trim()] = $value.Trim()
    }
}
$database = $settings["POSTGRES_DB"]
$user = $settings["POSTGRES_USER"]
if (-not $database -or -not $user) {
    throw "POSTGRES_DB e POSTGRES_USER devem estar definidos em .env.docker."
}

$resolvedOutput = [IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
New-Item -ItemType Directory -Path $resolvedOutput -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMddTHHmmss"
$fileName = "controle-bancario-postgres-$timestamp.dump"
$outputFile = Join-Path $resolvedOutput $fileName
$containerFile = "/tmp/$fileName"

Push-Location $projectRoot
try {
    & $dockerExecutable compose --env-file $envFilePath exec -T postgres `
        pg_dump -U $user -d $database -Fc -f $containerFile
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump falhou."
    }
    & $dockerExecutable compose --env-file $envFilePath exec -T postgres `
        pg_restore --list $containerFile | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "A validação do dump falhou."
    }
    & $dockerExecutable compose --env-file $envFilePath cp `
        "postgres:$containerFile" $outputFile
    if ($LASTEXITCODE -ne 0) {
        throw "Não foi possível copiar o backup do contêiner."
    }
    & $dockerExecutable compose --env-file $envFilePath exec -T postgres `
        rm -f $containerFile
} finally {
    Pop-Location
}

$backup = Get-Item -LiteralPath $outputFile
if ($backup.Length -eq 0) {
    throw "O arquivo de backup foi criado vazio."
}
Write-Output "Backup PostgreSQL validado em $($backup.FullName)"
Write-Output "Tamanho: $($backup.Length) bytes"
