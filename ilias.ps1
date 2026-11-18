$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\ilias_sync.py" @args
exit $LASTEXITCODE
