param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
