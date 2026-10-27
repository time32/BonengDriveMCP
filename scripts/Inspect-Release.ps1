param(
    [string]$ReleaseDir = (Join-Path $PSScriptRoot '..\..'),
    [string]$OutputFile = (Join-Path $PSScriptRoot '..\data\assembly_inventory.json')
)

# Run with Windows PowerShell 5.1. Reflection-only loading never invokes application code.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ReleaseDir = [IO.Path]::GetFullPath($ReleaseDir)
$OutputFile = [IO.Path]::GetFullPath($OutputFile)
if ($PSVersionTable.PSEdition -eq 'Core') {
    throw 'Use Windows PowerShell 5.1 (powershell.exe), not pwsh.exe.'
}

[AppDomain]::CurrentDomain.add_ReflectionOnlyAssemblyResolve({
    param($sender, $eventArgs)
    $name = New-Object Reflection.AssemblyName($eventArgs.Name)
    # Unify the old WinForms dependency to the installed .NET Framework version.
    if ($name.Name -match '^(System($|\.)|mscorlib$|Microsoft.CSharp$)') {
        $frameworkDll = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) ($name.Name + '.dll')
        if (Test-Path -LiteralPath $frameworkDll) {
            return [Reflection.Assembly]::ReflectionOnlyLoadFrom($frameworkDll)
        }
    }
    $localDll = Join-Path $ReleaseDir ($name.Name + '.dll')
    if (Test-Path -LiteralPath $localDll) {
        return [Reflection.Assembly]::ReflectionOnlyLoadFrom($localDll)
    }
    return [Reflection.Assembly]::ReflectionOnlyLoad($eventArgs.Name)
})

$assemblyPath = Join-Path $ReleaseDir 'BonengDrivesoft.exe'
$assembly = [Reflection.Assembly]::ReflectionOnlyLoadFrom($assemblyPath)
$loaderErrors = @()
try { $allTypes = $assembly.GetTypes() }
catch [Reflection.ReflectionTypeLoadException] {
    $allTypes = $_.Exception.Types | Where-Object { $_ }
    $loaderErrors = @($_.Exception.LoaderExceptions | ForEach-Object { $_.Message } | Select-Object -Unique)
}
$flags = [Reflection.BindingFlags]'Public,NonPublic,Instance,Static,DeclaredOnly'
$typeRecords = @(
    foreach ($type in $allTypes) {
        if ($type.FullName -notmatch '(?i)(communication\.|\.util\.|\.model\.|\.entity\.|\.data\.|\.trace\.|PagefileParam|Guide_|Download|Upgrade|MyATAccess)') { continue }
        [ordered]@{
            name = $type.FullName
            public = $type.IsPublic
            base_type = if ($type.BaseType) { $type.BaseType.FullName } else { $null }
            fields = @($type.GetFields($flags) | ForEach-Object {
                [ordered]@{ name = $_.Name; type = $_.FieldType.FullName; static = $_.IsStatic; public = $_.IsPublic }
            })
            methods = @($type.GetMethods($flags) | ForEach-Object {
                [ordered]@{ name = $_.Name; signature = $_.ToString(); static = $_.IsStatic; public = $_.IsPublic; metadata_token = $_.MetadataToken }
            })
        }
    }
)
$inventory = [ordered]@{
    schema_version = 1
    evidence_kind = 'reflection_only_metadata'
    application = 'BonengDrivesoft.exe'
    sha256 = (Get-FileHash -LiteralPath $assemblyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    assembly_version = $assembly.GetName().Version.ToString()
    target_runtime = $assembly.ImageRuntimeVersion
    device_calls_executed = $false
    type_count = @($allTypes).Count
    loader_errors = $loaderErrors
    references = @($assembly.GetReferencedAssemblies() | ForEach-Object { [ordered]@{ name = $_.Name; version = $_.Version.ToString() } })
    types = $typeRecords
}
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($OutputFile)) | Out-Null
[IO.File]::WriteAllText($OutputFile, ($inventory | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
Write-Output ('Wrote {0} types to {1}; no application methods executed.' -f $typeRecords.Count, $OutputFile)
