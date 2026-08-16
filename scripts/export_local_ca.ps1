param(
    [string]$SubjectPattern = "*Avast Web/Mail Shield Root*"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $projectRoot ".certs"
$outputFile = Join-Path $outputDir "local-root-ca.crt"

$certificate = Get-ChildItem Cert:\CurrentUser\Root, Cert:\LocalMachine\Root |
    Where-Object {
        $_.Subject -like $SubjectPattern -and $_.NotAfter -gt (Get-Date)
    } |
    Select-Object -First 1

if (-not $certificate) {
    throw "Nenhum certificado raiz corresponde a '$SubjectPattern'."
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$base64 = [Convert]::ToBase64String(
    $certificate.RawData,
    [Base64FormattingOptions]::InsertLineBreaks
)
$pem = "-----BEGIN CERTIFICATE-----`r`n$base64`r`n-----END CERTIFICATE-----`r`n"
[IO.File]::WriteAllText($outputFile, $pem, [Text.Encoding]::ASCII)

Write-Output "Certificado exportado para $outputFile"
Write-Output "Subject: $($certificate.Subject)"
Write-Output "Thumbprint: $($certificate.Thumbprint)"
