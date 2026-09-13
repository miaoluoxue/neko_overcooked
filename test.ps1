param(
    [string]$Log = ""
)

$ErrorActionPreference = "Continue"

if ([string]::IsNullOrWhiteSpace($Log)) {
    python -u run_engine.py --input virtual --mode none
    exit $LASTEXITCODE
}

python -u run_engine.py --input virtual --mode none 2>&1 | Tee-Object -FilePath $Log
exit $LASTEXITCODE
