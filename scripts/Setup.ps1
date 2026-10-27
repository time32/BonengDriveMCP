param([switch]$Analysis)

$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & python -m venv (Join-Path $project '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python venv creation failed. Install Python 3.11 or later.' }
}
& $python -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $project 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'MCP dependency installation failed.' }
if ($Analysis) {
    & $python -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $project 'requirements-analysis.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Catalog extractor dependency installation failed.' }
    & $python (Join-Path $PSScriptRoot 'build_catalog.py')
    if ($LASTEXITCODE -ne 0) { throw 'Catalog extraction failed.' }
}
& $python (Join-Path $PSScriptRoot 'write_codex_config.py')
if ($LASTEXITCODE -ne 0) { throw 'MCP connection configuration generation failed.' }
Write-Output 'Ready. Start with .venv\Scripts\python.exe -u run_server.py (stdio).'
