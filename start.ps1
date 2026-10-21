$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
}
& .\.venv\Scripts\python.exe -c "import importlib.util,sys; sys.exit(not all(importlib.util.find_spec(m) for m in ('flask','waitress','psycopg')))"
if ($LASTEXITCODE -ne 0) {
    & .\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check internet access.' }
}
& .\.venv\Scripts\python.exe app.py
