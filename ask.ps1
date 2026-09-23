param([Parameter(Position=0)][string]$Question, [switch]$Preview, [string[]]$Dataset)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $Question) { $Question = Read-Host 'Ask SDD' }
$taskArguments = @('-m', 'sdd.cli', 'ask', $Question)
foreach ($taskDataset in $Dataset) { $taskArguments += @('--dataset', $taskDataset) }
$env:PYTHONIOENCODING = 'utf-8'
if ($Preview) { $taskArguments += '--preview' }
& .\.venv\Scripts\python.exe @taskArguments
