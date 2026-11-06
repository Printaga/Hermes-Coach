# Run the same two gates as scripts/test.sh on Windows.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = $env:COACH_PYTHON
if (-not $python) { $python = "python" }

$status = 0
try {
  uv run --python "$python" --with "pytest==9.2.0" --with "pyyaml==6.0.2" `
    --with "fastapi==0.133.1" --with "httpx==0.28.1" `
    python -m pytest tests -q @args
  if ($LASTEXITCODE -ne 0) { $status = $LASTEXITCODE }
} catch {
  Write-Error $_ -ErrorAction Continue
  $status = 1
}

if ($env:SKIP_DESKTOP_CHECK -ne "1") {
  try {
    node scripts/check-desktop-page.mjs
    if ($LASTEXITCODE -ne 0) { $status = $LASTEXITCODE }
  } catch {
    Write-Error $_ -ErrorAction Continue
    $status = 1
  }
} else {
  Write-Warning "desktop-page check skipped (SKIP_DESKTOP_CHECK=1)"
}

exit $status
