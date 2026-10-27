param([Parameter(Mandatory=$true)][string]$LaunchRequest)
$ErrorActionPreference = 'Stop'
$launch = Get-Content -LiteralPath $LaunchRequest -Raw -Encoding utf8 | ConvertFrom-Json
# WMI is the process parent, so closing a temporary MCP client's Job Object
# does not kill an explicitly requested continuous device-control worker.
# ShowWindow=0 keeps the helper hidden; no service or scheduled task is installed.
$startupClass = Get-CimClass -ClassName Win32_ProcessStartup
$startup = New-CimInstance -CimClass $startupClass -ClientOnly -Property @{ShowWindow=[UInt16]0}
$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine=[string]$launch.command_line
    CurrentDirectory=[string]$launch.cwd
    ProcessStartupInformation=$startup
}
if ($result.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($result.ReturnValue)" }
@{pid=[int]$result.ProcessId; launcher='Win32_Process'; hidden=$true} | ConvertTo-Json -Compress
