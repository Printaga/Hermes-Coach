# Run the same two gates as scripts/test.sh on Windows.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = $env:COACH_PYTHON
if (-not $python) { $python = "python3" }

$status = 0
try {
  uv run --python $python --with pytest --with pyyaml python -m pytest tests -q @args
  if ($LASTEXITCODE -ne 0) { $status = $LASTEXITCODE }
} catch {
  Write-Error $_
  $status = 1
}

if ($env:SKIP_DESKTOP_CHECK -ne "1") {
  node scripts/check-desktop-page.mjs
  if ($LASTEXITCODE -ne 0) { $status = $LASTEXITCODE }
}

exit $status
