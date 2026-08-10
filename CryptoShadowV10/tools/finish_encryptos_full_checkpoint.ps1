param(
    [int]$CheckpointProcessId = 15892
)

$ErrorActionPreference = 'Stop'
$root = 'C:\v10'
$stdout = Join-Path $root 'logs\encryptos_full_checkpoint.out.log'
$stderr = Join-Path $root 'logs\encryptos_full_checkpoint.err.log'
$database = Join-Path $root 'data\telegram_research\sources\encryptos_complete\index.sqlite'
$ledger = Join-Path $root 'data\telegram_research\sources\encryptos_complete\checkpoints\analysis_ledger.jsonl'
$log = Join-Path $root 'logs\encryptos_full_index.log'

while (Get-Process -Id $CheckpointProcessId -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 10
}

if ((Test-Path $stderr) -and (Get-Item $stderr).Length -gt 0) {
    Add-Content -LiteralPath $log -Value "Checkpoint terminou com erro; indexacao nao iniciada."
    exit 1
}

$manifest = (Get-Content -LiteralPath $stdout -Tail 1).Trim()
if (-not $manifest -or -not (Test-Path -LiteralPath $manifest)) {
    Add-Content -LiteralPath $log -Value "Manifesto integral nao encontrado em $stdout"
    exit 2
}

Set-Location -LiteralPath $root
python tools\telegram_research_index.py $manifest $database $ledger *>> $log
