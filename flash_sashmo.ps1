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
$configPath = Join-Path $root 'config.local.json'
$sketch = Join-Path $root 'firmware\Sashmo'
$header = Join-Path $sketch 'config_private.h'
$build = Join-Path $root 'build\sashmo'
$wifiPath = Join-Path $root 'wifipassword.txt'

foreach ($path in @($cli, $configPath, $wifiPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required local file is missing: $path"
    }
}

$localConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$password = [IO.File]::ReadAllText($wifiPath, [Text.Encoding]::UTF8).TrimEnd([char[]]@("`r", "`n"))

function Assert-SingleLine([string]$value, [string]$name) {
    if ([string]::IsNullOrEmpty($value) -or $value.Contains("`r") -or $value.Contains("`n") -or $value.Contains([char]0)) {
        throw "$name must be a nonempty single line."
    }
}

function C-Bytes([string]$value) {
    $bytes = @([Text.Encoding]::UTF8.GetBytes($value) | ForEach-Object { '0x{0:X2}' -f $_ }) + '0x00'
    return '{ ' + ($bytes -join ', ') + ' }'
}

Assert-SingleLine $password 'Wi-Fi password'
Assert-SingleLine ([string]$localConfig.wifi_ssid) 'Wi-Fi SSID'
Assert-SingleLine ([string]$localConfig.device_token) 'Device token'
Assert-SingleLine ([string]$localConfig.bind_host) 'Server host'

$usb = if ($null -eq $localConfig.usb_pulse_seconds) { 30 } else { [int]$localConfig.usb_pulse_seconds }
$battery = if ($null -eq $localConfig.battery_pulse_seconds) { 60 } else { [int]$localConfig.battery_pulse_seconds }
$lowPower = if ($null -eq $localConfig.low_power_pulse_seconds) { 300 } else { [int]$localConfig.low_power_pulse_seconds }
foreach ($interval in @($usb, $battery, $lowPower)) {
    if ($interval -lt 1 -or $interval -gt 3600) { throw 'Pulse intervals must be 1 through 3600 seconds.' }
}
$usbBrightness = if ($null -eq $localConfig.usb_brightness) { 180 } else { [int]$localConfig.usb_brightness }
$batteryBrightness = if ($null -eq $localConfig.battery_brightness) { 130 } else { [int]$localConfig.battery_brightness }
$batteryIdleBrightness = if ($null -eq $localConfig.battery_idle_brightness) { 75 } else { [int]$localConfig.battery_idle_brightness }
$lowBatteryBrightness = if ($null -eq $localConfig.low_battery_brightness) { 45 } else { [int]$localConfig.low_battery_brightness }
foreach ($brightness in @($usbBrightness, $batteryBrightness, $batteryIdleBrightness, $lowBatteryBrightness)) {
    if ($brightness -lt 1 -or $brightness -gt 255) { throw 'Brightness settings must be 1 through 255.' }
}
$batteryDimAfter = if ($null -eq $localConfig.battery_dim_after_seconds) { 45 } else { [int]$localConfig.battery_dim_after_seconds }
if ($batteryDimAfter -lt 1 -or $batteryDimAfter -gt 3600) { throw 'Battery dim delay must be 1 through 3600 seconds.' }
$screenStandbyAfter = if ($null -eq $localConfig.screen_standby_after_seconds) { 300 } else { [int]$localConfig.screen_standby_after_seconds }
if ($screenStandbyAfter -lt 60 -or $screenStandbyAfter -gt 3600) { throw 'Display standby delay must be 60 through 3600 seconds.' }
$lowBatteryPercent = if ($null -eq $localConfig.low_battery_percent) { 15 } else { [int]$localConfig.low_battery_percent }
if ($lowBatteryPercent -lt 1 -or $lowBatteryPercent -gt 50) { throw 'Low-battery threshold must be 1 through 50 percent.' }
$wakeOnBattery = if ($null -eq $localConfig.wake_on_battery) { $false } else { [bool]$localConfig.wake_on_battery }

$lines = @(
    '#pragma once'
    ('static const char kWifiSsid[] = ' + (C-Bytes ([string]$localConfig.wifi_ssid)) + ';')
    ('static const char kWifiPassword[] = ' + (C-Bytes $password) + ';')
    ('static const char kDeviceToken[] = ' + (C-Bytes ([string]$localConfig.device_token)) + ';')
    ('static const char kServerHost[] = ' + (C-Bytes ([string]$localConfig.bind_host)) + ';')
    ('constexpr int kServerPort = {0};' -f [int]$localConfig.port)
    ('constexpr uint32_t kUsbPulseSeconds = {0};' -f $usb)
    ('constexpr uint32_t kBatteryPulseSeconds = {0};' -f $battery)
    ('constexpr uint32_t kLowPowerPulseSeconds = {0};' -f $lowPower)
    ('constexpr uint8_t kUsbBrightness = {0};' -f $usbBrightness)
    ('constexpr uint8_t kBatteryBrightness = {0};' -f $batteryBrightness)
    ('constexpr uint8_t kBatteryIdleBrightness = {0};' -f $batteryIdleBrightness)
    ('constexpr uint8_t kLowBatteryBrightness = {0};' -f $lowBatteryBrightness)
    ('constexpr uint32_t kBatteryDimAfterSeconds = {0};' -f $batteryDimAfter)
    ('constexpr uint32_t kScreenStandbyAfterSeconds = {0};' -f $screenStandbyAfter)
    ('constexpr int kLowBatteryPercent = {0};' -f $lowBatteryPercent)
    ('constexpr bool kWakeOnBattery = {0};' -f $wakeOnBattery.ToString().ToLowerInvariant())
)
[IO.File]::WriteAllText($header, ($lines -join "`n") + "`n", [Text.Encoding]::ASCII)

Push-Location $root
try {
    & $cli --config-file (Join-Path $root 'arduino-cli.yaml') compile --fqbn 'm5stack:esp32:m5stack_cores3' --output-dir $build $sketch
    if ($LASTEXITCODE -ne 0) { throw 'Sashmo compilation failed.' }

    if (-not $BuildOnly) {
        & $cli --config-file (Join-Path $root 'arduino-cli.yaml') upload --port $Port --fqbn 'm5stack:esp32:m5stack_cores3' --input-dir $build $sketch
        if ($LASTEXITCODE -ne 0) { throw 'Sashmo upload failed.' }
    }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $header -Force -ErrorAction SilentlyContinue
}
