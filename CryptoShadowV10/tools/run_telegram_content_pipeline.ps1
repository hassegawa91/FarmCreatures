$ErrorActionPreference = 'Stop'

$researchPython = 'C:\v10\.research-venv\Scripts\python.exe'
$pipeline = 'C:\v10\tools\telegram_media_pipeline.py'
$database = 'C:\v10\data\telegram_research\encryptos_research.sqlite'
$ledger = 'C:\v10\data\telegram_research\checkpoints\analysis_ledger.jsonl'
$checkpoint = 'encryptos-20260803T225828Z-p107'

# Keep enough CPU available for the live scanner and dashboard.
$env:OMP_NUM_THREADS = '2'
$env:OMP_THREAD_LIMIT = '2'
$env:MKL_NUM_THREADS = '2'
$env:OPENBLAS_NUM_THREADS = '2'
$env:CT2_NUM_THREADS = '2'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'

& $researchPython $pipeline $database $ledger $checkpoint transcribe --model small --beam-size 1 --cpu-threads 2
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $researchPython $pipeline $database $ledger $checkpoint video-frames --frame-interval 15
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $researchPython $pipeline $database $ledger $checkpoint images
exit $LASTEXITCODE
