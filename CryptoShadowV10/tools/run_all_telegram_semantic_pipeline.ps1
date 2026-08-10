$ErrorActionPreference = 'Stop'
$root = 'C:\v10'
$python = Join-Path $root '.research-venv\Scripts\python.exe'
$masterLog = Join-Path $root 'logs\telegram_semantic_all'
New-Item -ItemType Directory -Path $masterLog -Force | Out-Null
Set-Location -LiteralPath $root

$jobs = @(
    @{ Name='encryptos_complete'; Checkpoint='encryptos_full_media-20260805T033023Z-p751' },
    @{ Name='rose_signals'; Checkpoint='rose_signals-20260805T015736Z-p44' },
    @{ Name='killers'; Checkpoint='killers-20260805T014932Z-p4' },
    @{ Name='bullish_traders'; Checkpoint='bullish_traders-20260805T015813Z-p5' },
    @{ Name='crypto_sharks'; Checkpoint='crypto_sharks-20260805T014947Z-p8' },
    @{ Name='liquidations'; Checkpoint='liquidations-20260805T014832Z-p219' },
    @{ Name='pump_dump'; Checkpoint='pump_dump-20260805T014821Z-p210' },
    @{ Name='alerts_futures'; Checkpoint='alerts_futures-20260805T014933Z-p1' }
)

function Run-LowPriorityStage {
    param(
        [string]$JobName,
        [string]$StageName,
        [string]$Database,
        [string]$Ledger,
        [string]$Checkpoint,
        [string[]]$StageArguments
    )
    $logDir = Join-Path $masterLog $JobName
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $done = Join-Path $logDir "$StageName.done"
    if (Test-Path -LiteralPath $done) { return }
    $stdout = Join-Path $logDir "$StageName.out.log"
    $stderr = Join-Path $logDir "$StageName.err.log"
    $base = @('tools\telegram_media_pipeline.py', $Database, $Ledger, $Checkpoint)
    $process = Start-Process -FilePath $python -ArgumentList ($base + $StageArguments) `
        -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -PassThru
    try { $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::Idle } catch {}
    $process.WaitForExit()
    if (-not (Test-Path -LiteralPath $stdout) -or (Get-Item $stdout).Length -eq 0) {
        throw "$JobName/$StageName falhou; consulte $stderr"
    }
    Set-Content -LiteralPath $done -Value (Get-Date).ToString('o')
}

function Run-WithRetry {
    param([scriptblock]$Action, [string]$Label)
    while ($true) {
        try {
            & $Action
            return
        } catch {
            Add-Content -LiteralPath (Join-Path $masterLog 'retry.log') `
                -Value "$(Get-Date -Format o) $Label $($_.Exception.Message)"
            Start-Sleep -Seconds 60
        }
    }
}

foreach ($job in $jobs) {
    $sourceDir = Join-Path $root "data\telegram_research\sources\$($job.Name)"
    $database = Join-Path $sourceDir 'index.sqlite'
    $checkpointDir = Join-Path $sourceDir 'checkpoints'
    New-Item -ItemType Directory -Path $checkpointDir -Force | Out-Null
    $ledger = Join-Path $checkpointDir 'analysis_ledger.jsonl'

    Run-WithRetry { Run-LowPriorityStage $job.Name '01_documents' $database $ledger $job.Checkpoint @('documents') } "$($job.Name)/documents"
    Run-WithRetry { Run-LowPriorityStage $job.Name '02_transcripts' $database $ledger $job.Checkpoint @('transcribe','--model','small','--beam-size','1','--cpu-threads','1') } "$($job.Name)/transcripts"
    Run-WithRetry { Run-LowPriorityStage $job.Name '03_video_frames' $database $ledger $job.Checkpoint @('video-frames','--frame-interval','30') } "$($job.Name)/video_frames"
    Run-WithRetry { Run-LowPriorityStage $job.Name '04_images' $database $ledger $job.Checkpoint @('images') } "$($job.Name)/images"

    $reportDone = Join-Path (Join-Path $masterLog $job.Name) '05_strategy_report.done'
    if (-not (Test-Path -LiteralPath $reportDone)) {
        $evidence = Join-Path $sourceDir 'evidence_after_full_media.jsonl'
        $report = Join-Path $sourceDir 'strategy_report_after_full_media.md'
        Run-WithRetry {
            $out = Join-Path (Join-Path $masterLog $job.Name) '05_strategy_report.out.log'
            $err = Join-Path (Join-Path $masterLog $job.Name) '05_strategy_report.err.log'
            $process = Start-Process -FilePath $python -ArgumentList @(
                'tools\telegram_strategy_miner.py', $database, $evidence, $report
            ) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $out `
              -RedirectStandardError $err -PassThru
            try { $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::Idle } catch {}
            $process.WaitForExit()
            if (-not (Test-Path -LiteralPath $out)) { throw "$($job.Name)/strategy_report falhou" }
        } "$($job.Name)/strategy_report"
        Set-Content -LiteralPath $reportDone -Value (Get-Date).ToString('o')
    }
}

Set-Content -LiteralPath (Join-Path $masterLog 'ALL_MEDIA_COMPLETE.done') -Value (Get-Date).ToString('o')
