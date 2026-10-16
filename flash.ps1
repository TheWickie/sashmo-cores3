param(
    [string]$Port = 'COM4',
    [switch]$BuildOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cli = Join-Path $root 'tools\arduino-cli.exe'
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) {
    $installedCli = Get-Command arduino-cli.exe -ErrorAction SilentlyContinue
    if (-not $installedCli) { throw 'Install Arduino CLI or place arduino-cli.exe in tools.' }
    $cli = $installedCli.Source
}
$config = Join-Path $root 'arduino-cli.yaml'
$sketch = Join-Path $root 'WifiStatus'
$header = Join-Path $sketch 'wifi_credentials.h'
$build = Join-Path $root 'build'
$passwordFile = Join-Path $root 'wifipassword.txt'
$configPath = Join-Path $root 'config.local.json'

if (-not (Test-Path -LiteralPath $passwordFile -PathType Leaf)) {
    throw 'Place the Wi-Fi password in wifipassword.txt before building.'
}
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Create config.local.json before building.'
}

$localConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$ssid = [string]$localConfig.wifi_ssid
$password = [IO.File]::ReadAllText($passwordFile, [Text.Encoding]::UTF8).TrimEnd([char[]]@("`r", "`n"))
if ([string]::IsNullOrEmpty($ssid) -or $ssid.Contains("`r") -or $ssid.Contains("`n") -or $ssid.Contains([char]0)) {
    throw 'config.local.json must contain a nonempty single-line wifi_ssid.'
}
if ($password.Length -eq 0 -or $password.Contains("`r") -or $password.Contains("`n") -or $password.Contains([char]0)) {
    throw 'wifipassword.txt must contain one nonempty password line.'
}

$ssidValues = @([Text.Encoding]::UTF8.GetBytes($ssid) | ForEach-Object { '0x{0:X2}' -f $_ }) + '0x00'
$values = @([Text.Encoding]::UTF8.GetBytes($password) | ForEach-Object { '0x{0:X2}' -f $_ }) + '0x00'
$content = "#pragma once`nstatic const char kWifiSsid[] = { $($ssidValues -join ', ') };`nstatic const char kWifiPassword[] = { $($values -join ', ') };`n"
[IO.File]::WriteAllText($header, $content, [Text.Encoding]::ASCII)

Push-Location $root
try {
    & $cli --config-file $config compile --fqbn 'm5stack:esp32:m5stack_cores3' --output-dir $build $sketch
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed.' }

    if (-not $BuildOnly) {
        & $cli --config-file $config upload --port $Port --fqbn 'm5stack:esp32:m5stack_cores3' --input-dir $build $sketch
        if ($LASTEXITCODE -ne 0) { throw 'Upload failed.' }
    }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $header -Force -ErrorAction SilentlyContinue
}
