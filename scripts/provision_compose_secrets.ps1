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

function New-UrlSafeSecret {
    param([int]$ByteCount = 48)

    $bytes = [byte[]]::new($ByteCount)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Assert-SecretValue {
    param(
        [string]$Name,
        [AllowNull()][string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name deve estar definido e não vazio no arquivo de ambiente."
    }
    if ($Value -match '[\r\n]') {
        throw "$Name não pode conter quebras de linha."
    }

    # Evita transformar o .env.docker.example em uma credencial operacional
    # por acidente. O teste é deliberadamente restrito para não impor uma
    # política de formato além da validação feita pela aplicação.
    if ($Value.Trim() -match '^(troque-por-|change[-_ ]?me$|replace[-_ ]?me$|changeme$)') {
        throw "$Name ainda usa um valor-placeholder; defina um segredo real antes da provisão."
    }
}

$secretSources = [ordered]@{
    "django_secret_key" = "DJANGO_SECRET_KEY"
    "postgres_password" = "POSTGRES_PASSWORD"
}

foreach ($fileName in $secretSources.Keys) {
    $destination = Join-Path $secretsPath $fileName
    # Um segredo que já está no diretório é a fonte de verdade. Isso permite
    # manter uma instalação existente quando o .env foi sanitizado ou deixou
    # de carregar segredos, sem substituir o valor por vazio ou placeholder.
    if ($Force -or -not (Test-Path -LiteralPath $destination)) {
        Assert-SecretValue -Name $secretSources[$fileName] -Value $settings[$secretSources[$fileName]]
    }
}

# O token da integração de patrimônio é a única credencial daqui que ninguém
# precisa escolher: ela não é compartilhada com um humano nem com outro serviço
# já instalado -- é entregue ao consolidador depois de gerada. Por isso ela é
# gerada aqui quando o arquivo de ambiente não a traz, em vez de recusar a
# provisão. Quem quiser fixar um valor (rotação coordenada com o consolidador,
# por exemplo) define PATRIMONIO_TOKEN no arquivo de ambiente.
$patrimonioPath = Join-Path $secretsPath "patrimonio_token"
if (($Force -or -not (Test-Path -LiteralPath $patrimonioPath)) -and
    (-not $settings.ContainsKey("PATRIMONIO_TOKEN") -or [string]::IsNullOrWhiteSpace($settings["PATRIMONIO_TOKEN"]))) {
    $settings["PATRIMONIO_TOKEN"] = New-UrlSafeSecret
} elseif ($Force -or -not (Test-Path -LiteralPath $patrimonioPath)) {
    Assert-SecretValue -Name "PATRIMONIO_TOKEN" -Value $settings["PATRIMONIO_TOKEN"]
}
$secretSources["patrimonio_token"] = "PATRIMONIO_TOKEN"

# A suíte roda em um banco efêmero e nunca precisa receber os segredos
# operacionais. Estes valores são sempre novos e vivem em arquivos distintos;
# não são lidos do .env nem reaproveitados pelos serviços de runtime.
$settings["QUALITY_DJANGO_SECRET_KEY"] = New-UrlSafeSecret
$settings["QUALITY_POSTGRES_PASSWORD"] = New-UrlSafeSecret
$settings["QUALITY_PATRIMONIO_TOKEN"] = New-UrlSafeSecret
$secretSources["quality_django_secret_key"] = "QUALITY_DJANGO_SECRET_KEY"
$secretSources["quality_postgres_password"] = "QUALITY_POSTGRES_PASSWORD"
$secretSources["quality_patrimonio_token"] = "QUALITY_PATRIMONIO_TOKEN"

# Faça toda a validação de destino antes da primeira escrita. Sem este
# preflight, um arquivo já existente no fim da enumeração poderia deixar a
# provisão pela metade, misturando uma rotação nova com credenciais antigas.
$destinations = @($secretSources.Keys | ForEach-Object { Join-Path $secretsPath $_ })
$destinations += @(
    (Join-Path $secretsPath "quality_django_secret_key"),
    (Join-Path $secretsPath "quality_postgres_password"),
    (Join-Path $secretsPath "quality_patrimonio_token")
)
$invalidDestinations = @($destinations | Where-Object { Test-Path -LiteralPath $_ -PathType Container })
if ($invalidDestinations.Count -gt 0) {
    $names = $invalidDestinations | ForEach-Object { Split-Path -Leaf $_ }
    throw "O destino de segredo é um diretório: $($names -join ', ')."
}

New-Item -ItemType Directory -Path $secretsPath -Force | Out-Null
$utf8NoBom = [Text.UTF8Encoding]::new($false)

foreach ($fileName in $secretSources.Keys) {
    $destination = Join-Path $secretsPath $fileName
    if ((Test-Path -LiteralPath $destination) -and -not $Force) {
        continue
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
