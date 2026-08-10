param(
    [string]$CheckpointId = 'encryptos_full_media-20260805T033023Z-p751'
)

$ErrorActionPreference = 'Stop'
$root = 'C:\v10'
$python = Join-Path $root '.research-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'C:\Users\BOT1\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }
$database = Join-Path $root 'data\telegram_research\sources\encryptos_complete\index.sqlite'
$ledger = Join-Path $root 'data\telegram_research\sources\encryptos_complete\checkpoints\analysis_ledger.jsonl'
$logDir = Join-Path $root 'logs\encryptos_semantic'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location -LiteralPath $root

function Run-LowPriorityStage {
    param([string]$Name, [string[]]$Arguments)
    $done = Join-Path $logDir "$Name.done"
    if (Test-Path -LiteralPath $done) { return }
    $stdout = Join-Path $logDir "$Name.out.log"
    $stderr = Join-Path $logDir "$Name.err.log"
    $process = Start-Process -FilePath $python -ArgumentList $Arguments -WorkingDirectory $root `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    try { $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::Idle } catch {}
    $process.WaitForExit()
    if (-not (Test-Path -LiteralPath $stdout) -or (Get-Item -LiteralPath $stdout).Length -eq 0) {
        throw "$Name terminou sem resultado. Consulte $stderr"
    }
    Set-Content -LiteralPath $done -Value (Get-Date).ToString('o')
}

$base = @('tools\telegram_media_pipeline.py', $database, $ledger, $CheckpointId)
Run-LowPriorityStage '01_documents' ($base + @('documents'))
Run-LowPriorityStage '02_transcripts' ($base + @('transcribe', '--model', 'small', '--beam-size', '1', '--cpu-threads', '1'))
Run-LowPriorityStage '03_video_frames' ($base + @('video-frames', '--frame-interval', '30'))
Run-LowPriorityStage '04_images' ($base + @('images'))

$reportDone = Join-Path $logDir '05_strategy_report.done'
if (-not (Test-Path -LiteralPath $reportDone)) {
    $evidence = Join-Path $root 'data\telegram_research\sources\encryptos_complete\evidence_after_full_media.jsonl'
    $report = Join-Path $root 'data\telegram_research\sources\encryptos_complete\strategy_report_after_full_media.md'
    $process = Start-Process -FilePath $python -ArgumentList @(
        'tools\telegram_strategy_miner.py', $database, $evidence, $report
    ) -WorkingDirectory $root -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logDir '05_strategy_report.out.log') `
      -RedirectStandardError (Join-Path $logDir '05_strategy_report.err.log') -PassThru
    try { $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::Idle } catch {}
    $process.WaitForExit()
    if (-not (Test-Path -LiteralPath (Join-Path $logDir '05_strategy_report.out.log'))) {
        throw 'Geracao do relatorio final falhou.'
    }
    Set-Content -LiteralPath $reportDone -Value (Get-Date).ToString('o')
}
