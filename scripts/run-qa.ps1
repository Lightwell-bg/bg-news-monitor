[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$TaskFile = 'docs/current-task.md'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Native exit codes are handled explicitly, including on PowerShell 7.
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = Split-Path -Parent $PSScriptRoot
$exitCode = 1
$locationPushed = $false
$previousConsoleInputEncoding = [Console]::InputEncoding
$previousConsoleOutputEncoding = [Console]::OutputEncoding
$previousOutputEncoding = $OutputEncoding

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

try {
    $claudeCommand = Get-Command claude -CommandType Application, ExternalScript -ErrorAction Stop |
        Select-Object -First 1

    $taskPath = if ([System.IO.Path]::IsPathRooted($TaskFile)) {
        $TaskFile
    } else {
        Join-Path -Path $projectRoot -ChildPath $TaskFile
    }
    if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf)) {
        throw "Task file not found: $taskPath"
    }
    $taskPath = (Resolve-Path -LiteralPath $taskPath).ProviderPath
    $taskName = Split-Path -Leaf $taskPath
    if ($taskName -like '.env*' -and $taskName -ne '.env.example') {
        throw 'A secret environment file cannot be used as TaskFile.'
    }

    Push-Location -LiteralPath $projectRoot
    $locationPushed = $true
    $runsPath = Join-Path -Path $projectRoot -ChildPath 'docs/runs'
    New-Item -ItemType Directory -Path $runsPath -Force | Out-Null
    $logPath = Join-Path -Path $runsPath -ChildPath ('qa-{0}.txt' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    # Reserve the filename without overwriting an earlier report.
    $logStream = [System.IO.File]::Open($logPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
    $logStream.Dispose()

    $prompt = @"
Прочитай AGENTS.md, CLAUDE.md, docs/architecture.md и файл задачи по абсолютному пути:
$taskPath
Это единственный источник текущей задачи для данного запуска.
Проведи тестирование после Developer. Изучи git diff, git diff --cached и относящиеся к задаче неотслеживаемые файлы, исключая .env и другие секретные файлы. Проверь критерии приёмки. Не меняй производственный код. Начни отчёт с PASS, FAIL или BLOCKED и укажи команды, результаты и шаги воспроизведения ошибок.
Соблюдай границы задачи и сохраняй пользовательские изменения.
Не запускай других агентов. Не выполняй commit, push или production-деплой.
Не читай .env и секретные .env.*; .env.example допустим только с пустыми значениями.
Не выводи секреты, токены или значения переменных окружения в prompt, отчёт или логи.
Не выдавай непроверенные результаты за работающие.
"@

    Write-Host "Report: $logPath"
    # Windows PowerShell may represent native stderr as ErrorRecord objects.
    # Continue keeps stderr in the combined textual report; LASTEXITCODE determines success.
    $ErrorActionPreference = 'Continue'
    & $claudeCommand.Source -p --agent qa --permission-mode auto --no-session-persistence $prompt 2>&1 |
        Tee-Object -FilePath $logPath -ErrorAction Stop
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($null -eq $exitCode) {
        throw 'Claude Code did not return an exit code.'
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
} finally {
    [Console]::InputEncoding = $previousConsoleInputEncoding
    [Console]::OutputEncoding = $previousConsoleOutputEncoding
    $OutputEncoding = $previousOutputEncoding
    if ($locationPushed) {
        Pop-Location
    }
}

exit $exitCode
