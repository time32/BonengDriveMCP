param([Parameter(Mandatory=$true)][string]$ReleaseDir)

$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$release = [IO.Path]::GetFullPath($ReleaseDir)
$original = Join-Path $release 'BonengDrivesoft.exe'
$expectedHash = '05b9be5e2e2a2cb74037c32451f89b0117dfa80983e4ff11b7b7c67912553f47'
if ((Get-FileHash -LiteralPath $original -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'This bridge supports only the inspected Drivesoft V1.28 executable.'
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework C# compiler is unavailable.' }
$outputDir = Join-Path $project 'workspace\reuse_bridge'
New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$output = Join-Path $outputDir 'ReuseReadProbe.exe'
$source = Join-Path $PSScriptRoot 'dotnet\ReuseReadProbe.cs'
& $compiler /nologo /target:exe /platform:x86 /reference:System.Web.Extensions.dll "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw 'Read-only bridge compilation failed.' }
$upgradeOutput = Join-Path $outputDir 'ReuseFirmwareUpgrade.exe'
$upgradeSource = Join-Path $PSScriptRoot 'dotnet\ReuseFirmwareUpgrade.cs'
& $compiler /nologo /target:exe /platform:x86 /reference:System.Web.Extensions.dll /reference:System.Windows.Forms.dll "/out:$upgradeOutput" $upgradeSource
if ($LASTEXITCODE -ne 0) { throw 'Vendor firmware bridge compilation failed.' }
$deviceOutput = Join-Path $outputDir 'ReuseDeviceBridge.exe'
& $compiler /nologo /target:exe /platform:x86 /reference:System.Web.Extensions.dll "/out:$deviceOutput" (Join-Path $PSScriptRoot 'dotnet\ReuseDeviceBridge.cs')
if ($LASTEXITCODE -ne 0) { throw 'Device bridge compilation failed.' }
foreach ($asset in @('project\Msglen.txt', 'project\ServoPlatforms.txt', 'Aid\License.lic')) {
    $assetSource = Join-Path $release $asset
    if (Test-Path -LiteralPath $assetSource) {
        $destination = Join-Path $outputDir $asset
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $assetSource -Destination $destination
    }
}
Write-Output "Built $output, $upgradeOutput and $deviceOutput; original application and user configuration were not modified."
