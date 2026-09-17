param(
    [string]$EnvFile = ".env.docker",
    [string]$SecretsDirectory = ".secrets",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$envFilePath = [IO.Path]::GetFullPath((Join-Path $projectRoot $EnvFile))
$secretsPath = [IO.Path]::GetFullPath((Join-Path $projectRoot $SecretsDirectory))
$projectPrefix = $projectRoot.TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar

if (-not $envFilePath.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "EnvFile deve permanecer dentro do projeto."
}
if (-not $secretsPath.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "SecretsDirectory deve permanecer dentro do projeto."
}
if (-not (Test-Path -LiteralPath $envFilePath -PathType Leaf)) {
    throw "Arquivo de ambiente não encontrado."
}

$settings = @{}
Get-Content -LiteralPath $envFilePath | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $name, $value = $line -split "=", 2
        if ($name) {
            $settings[$name.Trim()] = $value
        }
    }
}

$secretSources = @{
    "django_secret_key" = "DJANGO_SECRET_KEY"
    "postgres_password" = "POSTGRES_PASSWORD"
}

foreach ($source in $secretSources.Values) {
    if (-not $settings.ContainsKey($source) -or [string]::IsNullOrWhiteSpace($settings[$source])) {
        throw "$source deve estar definido e não vazio no arquivo de ambiente."
    }
}

# O token da integração de patrimônio é a única credencial daqui que ninguém
# precisa escolher: ela não é compartilhada com um humano nem com outro serviço
# já instalado -- é entregue ao consolidador depois de gerada. Por isso ela é
# gerada aqui quando o arquivo de ambiente não a traz, em vez de recusar a
# provisão. Quem quiser fixar um valor (rotação coordenada com o consolidador,
# por exemplo) define PATRIMONIO_TOKEN no arquivo de ambiente.
if (-not $settings.ContainsKey("PATRIMONIO_TOKEN") -or [string]::IsNullOrWhiteSpace($settings["PATRIMONIO_TOKEN"])) {
    $bytes = [byte[]]::new(48)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $settings["PATRIMONIO_TOKEN"] = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}
$secretSources["patrimonio_token"] = "PATRIMONIO_TOKEN"

New-Item -ItemType Directory -Path $secretsPath -Force | Out-Null
$utf8NoBom = [Text.UTF8Encoding]::new($false)

foreach ($fileName in $secretSources.Keys) {
    $destination = Join-Path $secretsPath $fileName
    if ((Test-Path -LiteralPath $destination) -and -not $Force) {
        throw "Arquivo de segredo já existe. Use -Force somente após confirmar a rotação."
    }

    $source = $secretSources[$fileName]
    $temporary = Join-Path $secretsPath ".$fileName.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporary, $settings[$source].Trim() + [Environment]::NewLine, $utf8NoBom)
        Move-Item -LiteralPath $temporary -Destination $destination -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

Write-Output "Arquivos de segredo provisionados. Eles permanecem ignorados pelo Git; não exiba nem versione seu conteúdo."
