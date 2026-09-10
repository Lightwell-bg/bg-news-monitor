#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only local readiness check for the AI Dev Team repository.

.DESCRIPTION
    Verifies required team files, local tooling and configuration syntax.
    The script is read-only: it never writes files, never installs anything,
    never uses the network and never starts an agent. Secret files such as
    .env are never read; ignore rules are probed with virtual paths only.

.PARAMETER Json
    Print a single JSON document to stdout instead of the readable table.

.EXAMPLE
    .\scripts\check-team.ps1

.EXAMPLE
    powershell -NoProfile -File C:\path\to\ai-dev-team\scripts\check-team.ps1 -Json

.NOTES
    Exit code 0 - every check passed.
    Exit code 1 - at least one check failed.
    Exit code 2 - internal technical error of the script.
#>
[CmdletBinding()]
param(
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:Results = New-Object System.Collections.Generic.List[object]
$script:GitPath = $null

$script:RequiredFiles = @(
    'AGENTS.md',
    'CLAUDE.md',
    'README.md',
    '.gitignore',
    '.claude/agents/developer.md',
    '.claude/agents/qa.md',
    '.codex/config.toml',
    '.codex/agents/release.toml',
    '.codex/agents/reviewer.toml',
    '.codex/agents/scout_docs.toml',
    'docs/architecture.md',
    'docs/current-task.md',
    'docs/decision-log.md',
    'docs/runs/.gitkeep',
    'scripts/check-team.ps1',
    'scripts/run-developer.ps1',
    'scripts/run-qa.ps1'
)

$script:RequiredDirectories = @(
    '.claude/agents',
    '.codex/agents',
    'docs',
    'docs/runs',
    'scripts'
)

$script:TomlFiles = @(
    '.codex/config.toml',
    '.codex/agents/reviewer.toml',
    '.codex/agents/scout_docs.toml',
    '.codex/agents/release.toml'
)

$script:PowerShellFiles = @(
    'scripts/run-developer.ps1',
    'scripts/run-qa.ps1'
)

# Expected effective behaviour of the repository .gitignore.
# The paths below are virtual: nothing is created and nothing is read.
$script:IgnoreExpectations = @(
    [pscustomobject]@{ Path = '.env'; ShouldBeIgnored = $true },
    [pscustomobject]@{ Path = '.env.local'; ShouldBeIgnored = $true },
    [pscustomobject]@{ Path = '.env.example'; ShouldBeIgnored = $false },
    [pscustomobject]@{ Path = 'docs/runs/test-report.txt'; ShouldBeIgnored = $true },
    [pscustomobject]@{ Path = 'docs/runs/.gitkeep'; ShouldBeIgnored = $false }
)

# Program used to identify a candidate interpreter.
$script:PythonProbeSource = @'
import sys
try:
    import tomllib
    has_tomllib = "1"
except Exception:
    has_tomllib = "0"
sys.stdout.write("PROBE %d.%d.%d %s\n" % (
    sys.version_info[0], sys.version_info[1], sys.version_info[2], has_tomllib))
'@

# Program that validates TOML syntax. File paths arrive through an environment
# variable, so no shell quoting is involved. Output is forced to ASCII.
$script:PythonTomlSource = @'
import os
import sys

try:
    import tomllib
except Exception:
    sys.stdout.write("TOMLLIB MISSING\n")
    sys.exit(0)

raw = os.environ.get("TEAMCHECK_TOML_FILES", "")
paths = [item for item in raw.split("|") if item.strip()]
for index, path in enumerate(paths):
    try:
        handle = open(path, "rb")
    except OSError as error:
        sys.stdout.write("FILE %d ERR cannot open file (%s)\n" % (index, type(error).__name__))
        continue
    try:
        tomllib.load(handle)
    except Exception as error:
        message = " ".join(str(error).split())[:200]
        message = message.encode("ascii", "replace").decode("ascii")
        sys.stdout.write("FILE %d ERR %s\n" % (index, message))
    else:
        sys.stdout.write("FILE %d OK\n" % index)
    finally:
        handle.close()
'@

function Get-ProjectPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    return (Join-Path -Path $script:ProjectRoot -ChildPath $RelativePath)
}

function Add-CheckResult {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Passed,
        [string]$Details = ''
    )

    $status = 'FAIL'
    if ($Passed) {
        $status = 'PASS'
    }
    $script:Results.Add([pscustomobject]@{
            Name    = $Name
            Status  = $status
            Details = $Details
        })
}

function ConvertTo-SingleLine {
    param([string]$Text, [int]$MaxLength = 200)

    if ([string]::IsNullOrEmpty($Text)) {
        return ''
    }
    $single = ($Text -replace '\s+', ' ').Trim()
    if ($single.Length -gt $MaxLength) {
        $single = $single.Substring(0, $MaxLength) + '...'
    }
    return $single
}

function ConvertTo-CommandLineArguments {
    param([string[]]$Arguments = @())

    if ($null -eq $Arguments -or $Arguments.Count -eq 0) {
        return ''
    }
    $parts = New-Object System.Collections.Generic.List[string]
    foreach ($argument in $Arguments) {
        $value = [string]$argument
        if ($value.Length -gt 0 -and $value -notmatch '[\s"]') {
            $parts.Add($value)
            continue
        }
        # Windows command line quoting: double every backslash run that precedes
        # a quote or the end of the argument, then escape the quotes themselves.
        $escaped = $value -replace '(\\+)("|$)', '$1$1$2'
        $escaped = $escaped -replace '"', '\"'
        $parts.Add('"' + $escaped + '"')
    }
    return ($parts -join ' ')
}

function Invoke-ExternalProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$StandardInput,
        [hashtable]$EnvironmentVariables,
        [int]$TimeoutMilliseconds = 20000
    )

    $useStandardInput = $PSBoundParameters.ContainsKey('StandardInput')

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ConvertTo-CommandLineArguments -Arguments $Arguments
    $startInfo.WorkingDirectory = $script:ProjectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    if ($useStandardInput) {
        $startInfo.RedirectStandardInput = $true
    }
    if ($PSBoundParameters.ContainsKey('EnvironmentVariables') -and $null -ne $EnvironmentVariables) {
        foreach ($key in $EnvironmentVariables.Keys) {
            $startInfo.EnvironmentVariables[[string]$key] = [string]$EnvironmentVariables[$key]
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $timedOut = $false
    $standardOutput = ''
    $standardError = ''
    $exitCode = -1

    try {
        $null = $process.Start()
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        if ($useStandardInput) {
            $process.StandardInput.Write($StandardInput)
            $process.StandardInput.Close()
        }
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $timedOut = $true
            try { $process.Kill() } catch { }
            try { $null = $process.WaitForExit(5000) } catch { }
        }
        try { $standardOutput = [string]$outputTask.Result } catch { $standardOutput = '' }
        try { $standardError = [string]$errorTask.Result } catch { $standardError = '' }
        if (-not $timedOut) {
            $exitCode = $process.ExitCode
        }
    } catch {
        return [pscustomobject]@{
            ExitCode = -1
            StdOut   = ''
            StdErr   = (ConvertTo-SingleLine -Text $_.Exception.Message -MaxLength 160)
            TimedOut = $false
            Started  = $false
        }
    } finally {
        $process.Dispose()
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        StdOut   = $standardOutput
        StdErr   = $standardError
        TimedOut = $timedOut
        Started  = $true
    }
}

# Microsoft Store "App Execution Alias" shims must never be started: launching one
# can open the Microsoft Store or an installer, which an offline read-only
# diagnostic is not allowed to do. Such shims live in a WindowsApps directory and
# are zero length stub files, so both signals are treated as unsafe. Anything that
# cannot be inspected as a regular non empty file is rejected as well.
function Test-IsUnsafePythonPath {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $true
    }
    $normalized = ([string]$Path) -replace '/', '\'
    $segments = @($normalized -split '\\' | Where-Object { $_ -ne '' })
    foreach ($segment in $segments) {
        if ($segment -eq 'WindowsApps') {
            return $true
        }
    }
    $item = $null
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch {
        return $true
    }
    if ($item -isnot [System.IO.FileInfo]) {
        return $true
    }
    if ($item.Length -eq 0) {
        return $true
    }
    return $false
}

function Get-PythonCandidateList {
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $candidates = New-Object System.Collections.Generic.List[object]
    $excludedAliases = New-Object System.Collections.Generic.List[string]

    function Add-PythonCandidate {
        param([string]$Path, [string[]]$PrefixArguments = @(), [string]$Source = '')

        if ([string]::IsNullOrWhiteSpace($Path)) {
            return
        }
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            return
        }
        $resolved = $Path
        try {
            $resolved = (Resolve-Path -LiteralPath $Path).ProviderPath
        } catch {
            $resolved = $Path
        }
        # Store aliases and stub files are dropped while the list is built, so they
        # can never become a candidate and can never be started later.
        if ((Test-IsUnsafePythonPath -Path $Path) -or (Test-IsUnsafePythonPath -Path $resolved)) {
            $excludedAliases.Add($resolved)
            return
        }
        $key = $resolved + '|' + ($PrefixArguments -join ' ')
        if (-not $seen.Add($key)) {
            return
        }
        $candidates.Add([pscustomobject]@{
                Path            = $resolved
                PrefixArguments = $PrefixArguments
                Source          = $Source
            })
    }

    $pathCommands = @()
    try {
        $pathCommands = @(Get-Command -Name 'python', 'python3' -CommandType Application -All -ErrorAction SilentlyContinue)
    } catch {
        $pathCommands = @()
    }

    # Only real interpreters from PATH. Microsoft Store aliases are rejected by
    # Add-PythonCandidate and are never executed.
    foreach ($command in $pathCommands) {
        Add-PythonCandidate -Path $command.Source -Source 'PATH'
    }

    $registryRoots = @(
        'HKCU:\Software\Python\PythonCore',
        'HKLM:\Software\Python\PythonCore',
        'HKLM:\Software\Wow6432Node\Python\PythonCore'
    )
    foreach ($registryRoot in $registryRoots) {
        if (-not (Test-Path -LiteralPath $registryRoot)) {
            continue
        }
        $versionKeys = @()
        try {
            $versionKeys = @(Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue |
                    Sort-Object -Property PSChildName -Descending)
        } catch {
            $versionKeys = @()
        }
        foreach ($versionKey in $versionKeys) {
            $installPath = $null
            try {
                $installProperty = Get-ItemProperty -LiteralPath (Join-Path -Path $versionKey.PSPath -ChildPath 'InstallPath') -ErrorAction Stop
                $installPath = [string]$installProperty.'(default)'
            } catch {
                $installPath = $null
            }
            if (-not [string]::IsNullOrWhiteSpace($installPath)) {
                Add-PythonCandidate -Path (Join-Path -Path $installPath -ChildPath 'python.exe') -Source ('registry ' + $versionKey.PSChildName)
            }
        }
    }

    $globPatterns = New-Object System.Collections.Generic.List[string]
    $localAppData = $env:LOCALAPPDATA
    if (-not [string]::IsNullOrWhiteSpace($localAppData)) {
        $globPatterns.Add((Join-Path -Path $localAppData -ChildPath 'Programs\Python\Python3*\python.exe'))
        $globPatterns.Add((Join-Path -Path $localAppData -ChildPath 'Python\pythoncore-*\python.exe'))
    }
    $programFiles = $env:ProgramFiles
    if (-not [string]::IsNullOrWhiteSpace($programFiles)) {
        $globPatterns.Add((Join-Path -Path $programFiles -ChildPath 'Python3*\python.exe'))
    }
    $programFilesX86 = ${env:ProgramFiles(x86)}
    if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
        $globPatterns.Add((Join-Path -Path $programFilesX86 -ChildPath 'Python3*\python.exe'))
    }
    $systemDrive = $env:SystemDrive
    if (-not [string]::IsNullOrWhiteSpace($systemDrive)) {
        $globPatterns.Add((Join-Path -Path ($systemDrive + '\') -ChildPath 'Python3*\python.exe'))
    }
    foreach ($pattern in $globPatterns) {
        $found = @()
        try {
            $found = @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
                    Sort-Object -Property FullName -Descending)
        } catch {
            $found = @()
        }
        foreach ($item in $found) {
            Add-PythonCandidate -Path $item.FullName -Source 'installation directory'
        }
    }

    $launcherCommands = @()
    try {
        $launcherCommands = @(Get-Command -Name 'py' -CommandType Application -All -ErrorAction SilentlyContinue)
    } catch {
        $launcherCommands = @()
    }
    foreach ($command in $launcherCommands) {
        Add-PythonCandidate -Path $command.Source -PrefixArguments @('-3') -Source 'py launcher'
    }

    # Plain arrays are returned on purpose: an array subexpression over a generic
    # list carried by a PSObject property is not reliable on every host.
    return [pscustomobject]@{
        Candidates      = $candidates.ToArray()
        ExcludedAliases = $excludedAliases.ToArray()
    }
}

function Resolve-PythonInterpreter {
    $enumeration = Get-PythonCandidateList
    $candidates = @($enumeration.Candidates)
    $excludedAliases = @($enumeration.ExcludedAliases)
    $probes = New-Object System.Collections.Generic.List[object]
    $maxProbes = 12
    $probeCount = 0
    $selected = $null

    foreach ($candidate in $candidates) {
        if ($probeCount -ge $maxProbes) {
            break
        }
        $probeCount++
        $arguments = New-Object System.Collections.Generic.List[string]
        foreach ($prefix in $candidate.PrefixArguments) {
            $arguments.Add($prefix)
        }
        # -I isolated mode, -B no bytecode files, - reads the program from stdin.
        $arguments.Add('-I')
        $arguments.Add('-B')
        $arguments.Add('-')

        $run = Invoke-ExternalProcess -FilePath $candidate.Path -Arguments $arguments.ToArray() -StandardInput $script:PythonProbeSource -TimeoutMilliseconds 20000
        $match = [regex]::Match($run.StdOut, 'PROBE\s+(\d+)\.(\d+)\.(\d+)\s+([01])')
        if (-not $match.Success) {
            $failureText = ConvertTo-SingleLine -Text ($run.StdErr + ' ' + $run.StdOut) -MaxLength 120
            if ($run.TimedOut) {
                $failureText = 'interpreter did not answer in time'
            }
            $probes.Add([pscustomobject]@{
                    Path       = $candidate.Path
                    Source     = $candidate.Source
                    Prefix     = $candidate.PrefixArguments
                    Usable     = $false
                    Version    = ''
                    Major      = 0
                    Minor      = 0
                    HasTomllib = $false
                    Error      = $failureText
                })
            continue
        }
        $major = [int]$match.Groups[1].Value
        $minor = [int]$match.Groups[2].Value
        $patch = [int]$match.Groups[3].Value
        $probe = [pscustomobject]@{
            Path       = $candidate.Path
            Source     = $candidate.Source
            Prefix     = $candidate.PrefixArguments
            Usable     = $true
            Version    = ('{0}.{1}.{2}' -f $major, $minor, $patch)
            Major      = $major
            Minor      = $minor
            HasTomllib = ($match.Groups[4].Value -eq '1')
            Error      = ''
        }
        $probes.Add($probe)
        # First safe interpreter that satisfies every requirement wins. Probing
        # stops here: no further candidate process is started.
        if ($probe.HasTomllib -and (($probe.Major -gt 3) -or ($probe.Major -eq 3 -and $probe.Minor -ge 11))) {
            $selected = $probe
            break
        }
    }

    # Nothing matched completely: report the closest interpreter that answered so
    # the failing check can explain why. No extra process is started here.
    if ($null -eq $selected) {
        $usable = @($probes | Where-Object { $_.Usable })
        $supported = @($usable | Where-Object { $_.Major -gt 3 -or ($_.Major -eq 3 -and $_.Minor -ge 11) })
        if ($supported.Count -gt 0) {
            $selected = $supported[0]
        } elseif ($usable.Count -gt 0) {
            $selected = $usable[0]
        }
    }

    return [pscustomobject]@{
        Selected        = $selected
        Probes          = $probes.ToArray()
        CandidateCount  = $candidates.Count
        ProbeCount      = $probeCount
        ExcludedAliases = $excludedAliases
    }
}

function Get-MarkdownFrontMatterLines {
    param([Parameter(Mandatory = $true)][string]$Path)

    $content = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrEmpty($content)) {
        return $null
    }
    $content = $content -replace "^\uFEFF", ''
    $lines = @($content -split "\r?\n")
    if ($lines.Count -eq 0 -or $lines[0].Trim() -ne '---') {
        return $null
    }
    $frontMatter = New-Object System.Collections.Generic.List[string]
    $closed = $false
    for ($index = 1; $index -lt $lines.Count; $index++) {
        if ($lines[$index].Trim() -eq '---') {
            $closed = $true
            break
        }
        $frontMatter.Add($lines[$index])
    }
    if (-not $closed) {
        return $null
    }
    return , $frontMatter.ToArray()
}

function ConvertFrom-SimpleYaml {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][AllowNull()][string[]]$Lines)

    $map = [ordered]@{}
    $currentKey = $null
    foreach ($line in $Lines) {
        if ($line -match '^\s*#') {
            continue
        }
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }
        if ($null -ne $currentKey -and $line -match '^\s+-\s*(.*)$') {
            $map[$currentKey].Items.Add($matches[1].Trim())
            continue
        }
        if ($line -match '^([A-Za-z0-9_.-]+)\s*:\s*(.*)$') {
            $key = $matches[1].Trim()
            $map[$key] = [pscustomobject]@{
                Value = $matches[2].Trim()
                Items = (New-Object System.Collections.Generic.List[string])
            }
            $currentKey = $key
            continue
        }
        $currentKey = $null
    }
    return $map
}

function Get-YamlStringList {
    param($Entry)

    $values = New-Object System.Collections.Generic.List[string]
    if ($null -eq $Entry) {
        return , $values.ToArray()
    }
    $raw = [string]$Entry.Value
    if (-not [string]::IsNullOrWhiteSpace($raw)) {
        $trimmed = $raw.Trim()
        if ($trimmed.StartsWith('[') -and $trimmed.EndsWith(']')) {
            $trimmed = $trimmed.Substring(1, $trimmed.Length - 2)
        }
        foreach ($part in ($trimmed -split ',')) {
            $item = $part.Trim().Trim('"').Trim("'").Trim()
            if (-not [string]::IsNullOrWhiteSpace($item)) {
                $values.Add($item)
            }
        }
    }
    foreach ($listItem in $Entry.Items) {
        $item = ([string]$listItem).Trim().Trim('"').Trim("'").Trim()
        if (-not [string]::IsNullOrWhiteSpace($item)) {
            $values.Add($item)
        }
    }
    return , $values.ToArray()
}

function Get-GrantedTools {
    param(
        [AllowEmptyCollection()][string[]]$Allowed = @(),
        [AllowEmptyCollection()][string[]]$Disallowed = @(),
        [Parameter(Mandatory = $true)][string[]]$Tools
    )

    if ($null -eq $Allowed) {
        $Allowed = @()
    }
    if ($null -eq $Disallowed) {
        $Disallowed = @()
    }
    # An explicit tools list grants exactly the tools it names. Without such a
    # list every tool is available, so only disallowedTools can revoke it.
    $restricted =($Allowed.Count -gt 0) -and (-not ($Allowed -contains '*')) -and (-not ($Allowed -contains 'All tools'))
    $granted = New-Object System.Collections.Generic.List[string]
    foreach ($tool in $Tools) {
        if ($restricted) {
            if ($Allowed -contains $tool) {
                $granted.Add($tool)
            }
        } elseif (-not ($Disallowed -contains $tool)) {
            $granted.Add($tool)
        }
    }
    return , $granted.ToArray()
}

function Test-GitIgnoredPath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    # core.excludesFile is cleared so that only the repository rules are measured.
    $arguments = @(
        '-C', $script:ProjectRoot,
        '-c', 'core.excludesFile=',
        'check-ignore', '--no-index', '-q', '--', $RelativePath
    )
    $run = Invoke-ExternalProcess -FilePath $script:GitPath -Arguments $arguments -TimeoutMilliseconds 15000
    if ($run.TimedOut) {
        return [pscustomobject]@{ Ignored = $false; Error = 'git check-ignore did not answer in time' }
    }
    if ($run.ExitCode -eq 0) {
        return [pscustomobject]@{ Ignored = $true; Error = '' }
    }
    if ($run.ExitCode -eq 1) {
        return [pscustomobject]@{ Ignored = $false; Error = '' }
    }
    $message = ConvertTo-SingleLine -Text ($run.StdErr + ' ' + $run.StdOut) -MaxLength 120
    if ([string]::IsNullOrWhiteSpace($message)) {
        $message = 'unexpected git exit code ' + $run.ExitCode
    }
    return [pscustomobject]@{ Ignored = $false; Error = $message }
}

function Invoke-FileChecks {
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($relative in $script:RequiredFiles) {
        if (-not (Test-Path -LiteralPath (Get-ProjectPath -RelativePath $relative) -PathType Leaf)) {
            $missing.Add($relative)
        }
    }
    if ($missing.Count -eq 0) {
        Add-CheckResult -Name 'Required team files' -Passed $true -Details ('{0} file(s) present' -f $script:RequiredFiles.Count)
    } else {
        Add-CheckResult -Name 'Required team files' -Passed $false -Details ('missing: ' + ($missing -join ', '))
    }
}

function Invoke-StructureCheck {
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($relative in $script:RequiredDirectories) {
        if (-not (Test-Path -LiteralPath (Get-ProjectPath -RelativePath $relative) -PathType Container)) {
            $missing.Add($relative)
        }
    }
    if (-not (Test-Path -LiteralPath (Get-ProjectPath -RelativePath 'docs/runs/.gitkeep') -PathType Leaf)) {
        $missing.Add('docs/runs/.gitkeep')
    }
    if ($missing.Count -eq 0) {
        Add-CheckResult -Name 'Base project structure' -Passed $true -Details ('{0} directory(ies) present, docs/runs kept by .gitkeep' -f $script:RequiredDirectories.Count)
    } else {
        Add-CheckResult -Name 'Base project structure' -Passed $false -Details ('missing: ' + ($missing -join ', '))
    }
}

function Get-ApplicationPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = $null
    try {
        $command = Get-Command -Name $Name -CommandType Application, ExternalScript -ErrorAction SilentlyContinue |
            Select-Object -First 1
    } catch {
        $command = $null
    }
    if ($null -eq $command) {
        return $null
    }
    return [string]$command.Source
}

function Invoke-ToolChecks {
    $script:GitPath = Get-ApplicationPath -Name 'git'
    if ($script:GitPath) {
        Add-CheckResult -Name 'Command: git' -Passed $true -Details $script:GitPath
    } else {
        Add-CheckResult -Name 'Command: git' -Passed $false -Details 'git was not found in PATH'
    }

    # Claude Code is only detected here. The diagnostic never starts an agent.
    $claudePath = Get-ApplicationPath -Name 'claude'
    if ($claudePath) {
        Add-CheckResult -Name 'Command: claude' -Passed $true -Details ($claudePath + ' (not executed)')
    } else {
        Add-CheckResult -Name 'Command: claude' -Passed $false -Details 'claude was not found in PATH'
    }
}

function Invoke-PythonChecks {
    param([Parameter(Mandatory = $true)]$PythonInfo)

    $selected = $PythonInfo.Selected
    if ($null -eq $selected) {
        $details = 'no usable local Python interpreter was found'
        if ($PythonInfo.CandidateCount -gt 0) {
            $details = ('{0} local candidate(s) probed, none answered correctly' -f $PythonInfo.ProbeCount)
        }
        if ($PythonInfo.ExcludedAliases.Count -gt 0) {
            $details = $details + ('; {0} Microsoft Store alias(es) skipped without running them' -f $PythonInfo.ExcludedAliases.Count)
        }
        Add-CheckResult -Name 'Command: python' -Passed $false -Details $details
        Add-CheckResult -Name 'Python version 3.11 or newer' -Passed $false -Details 'no usable Python interpreter was found'
        return
    }

    $launch = $selected.Path
    if ($selected.Prefix.Count -gt 0) {
        $launch = $launch + ' ' + ($selected.Prefix -join ' ')
    }
    Add-CheckResult -Name 'Command: python' -Passed $true -Details ('{0} [{1}]' -f $launch, $selected.Source)

    $supported = ($selected.Major -gt 3) -or ($selected.Major -eq 3 -and $selected.Minor -ge 11)
    if ($supported -and $selected.HasTomllib) {
        Add-CheckResult -Name 'Python version 3.11 or newer' -Passed $true -Details ('Python {0}, tomllib available' -f $selected.Version)
    } elseif ($supported) {
        Add-CheckResult -Name 'Python version 3.11 or newer' -Passed $false -Details ('Python {0} found, but tomllib cannot be imported' -f $selected.Version)
    } else {
        Add-CheckResult -Name 'Python version 3.11 or newer' -Passed $false -Details ('Python {0} is older than 3.11' -f $selected.Version)
    }
}

function Invoke-TomlChecks {
    param([Parameter(Mandatory = $true)]$PythonInfo)

    $selected = $PythonInfo.Selected
    $usable = $false
    if ($null -ne $selected) {
        $usable = $selected.HasTomllib -and (($selected.Major -gt 3) -or ($selected.Major -eq 3 -and $selected.Minor -ge 11))
    }
    if (-not $usable) {
        foreach ($relative in $script:TomlFiles) {
            Add-CheckResult -Name ('TOML syntax: ' + $relative) -Passed $false -Details 'no Python 3.11+ with tomllib available for parsing'
        }
        return
    }

    $absolutePaths = New-Object System.Collections.Generic.List[string]
    foreach ($relative in $script:TomlFiles) {
        $absolutePaths.Add((Get-ProjectPath -RelativePath $relative))
    }

    $arguments = New-Object System.Collections.Generic.List[string]
    foreach ($prefix in $selected.Prefix) {
        $arguments.Add($prefix)
    }
    $arguments.Add('-I')
    $arguments.Add('-B')
    $arguments.Add('-')

    $processParameters = @{
        FilePath             = $selected.Path
        Arguments            = $arguments.ToArray()
        StandardInput        = $script:PythonTomlSource
        EnvironmentVariables = @{ TEAMCHECK_TOML_FILES = ($absolutePaths -join '|') }
        TimeoutMilliseconds  = 30000
    }
    $run = Invoke-ExternalProcess @processParameters

    if ($run.TimedOut) {
        foreach ($relative in $script:TomlFiles) {
            Add-CheckResult -Name ('TOML syntax: ' + $relative) -Passed $false -Details 'Python did not answer in time'
        }
        return
    }

    $lines = @($run.StdOut -split "\r?\n")
    for ($index = 0; $index -lt $script:TomlFiles.Count; $index++) {
        $relative = $script:TomlFiles[$index]
        $marker = 'FILE ' + $index + ' '
        $line = @($lines | Where-Object { $_.StartsWith($marker) }) | Select-Object -First 1
        if ($null -eq $line) {
            $details = ConvertTo-SingleLine -Text ($run.StdErr + ' ' + $run.StdOut) -MaxLength 160
            if ([string]::IsNullOrWhiteSpace($details)) {
                $details = 'no answer from the TOML parser'
            }
            Add-CheckResult -Name ('TOML syntax: ' + $relative) -Passed $false -Details $details
            continue
        }
        $payload = $line.Substring($marker.Length).Trim()
        if ($payload -eq 'OK') {
            Add-CheckResult -Name ('TOML syntax: ' + $relative) -Passed $true -Details 'parsed by Python tomllib'
        } else {
            Add-CheckResult -Name ('TOML syntax: ' + $relative) -Passed $false -Details (ConvertTo-SingleLine -Text ($payload -replace '^ERR\s*', '') -MaxLength 160)
        }
    }
}

function Invoke-PowerShellSyntaxChecks {
    foreach ($relative in $script:PowerShellFiles) {
        $name = 'PowerShell syntax: ' + $relative
        $path = Get-ProjectPath -RelativePath $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            Add-CheckResult -Name $name -Passed $false -Details 'file not found'
            continue
        }
        $tokens = $null
        $parseErrors = $null
        try {
            $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
        } catch {
            Add-CheckResult -Name $name -Passed $false -Details (ConvertTo-SingleLine -Text $_.Exception.Message -MaxLength 160)
            continue
        }
        $errorList = @()
        if ($null -ne $parseErrors) {
            $errorList = @($parseErrors)
        }
        if ($errorList.Count -eq 0) {
            Add-CheckResult -Name $name -Passed $true -Details 'no parser errors'
        } else {
            $first = $errorList[0]
            $details = '{0} parser error(s), first at line {1}: {2}' -f $errorList.Count, $first.Extent.StartLineNumber, (ConvertTo-SingleLine -Text $first.Message -MaxLength 120)
            Add-CheckResult -Name $name -Passed $false -Details $details
        }
    }
}

function Invoke-QaAgentChecks {
    $memoryCheckName = 'QA frontmatter: no memory field'
    $toolsCheckName = 'QA frontmatter: Write and Edit not allowed'
    $relative = '.claude/agents/qa.md'
    $path = Get-ProjectPath -RelativePath $relative

    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-CheckResult -Name $memoryCheckName -Passed $false -Details ($relative + ' not found')
        Add-CheckResult -Name $toolsCheckName -Passed $false -Details ($relative + ' not found')
        return
    }

    $frontMatter = $null
    try {
        $frontMatter = Get-MarkdownFrontMatterLines -Path $path
    } catch {
        $frontMatter = $null
    }
    if ($null -eq $frontMatter) {
        $details = 'YAML frontmatter delimited by --- was not found'
        Add-CheckResult -Name $memoryCheckName -Passed $false -Details $details
        Add-CheckResult -Name $toolsCheckName -Passed $false -Details $details
        return
    }

    # Only the frontmatter is inspected: words in the instruction text are ignored.
    $map = ConvertFrom-SimpleYaml -Lines $frontMatter

    if ($map.Contains('memory')) {
        Add-CheckResult -Name $memoryCheckName -Passed $false -Details 'frontmatter declares a memory field'
    } else {
        Add-CheckResult -Name $memoryCheckName -Passed $true -Details 'frontmatter has no memory field'
    }

    $toolsEntry = $null
    if ($map.Contains('tools')) {
        $toolsEntry = $map['tools']
    }
    $disallowedEntry = $null
    if ($map.Contains('disallowedTools')) {
        $disallowedEntry = $map['disallowedTools']
    }
    # Get-YamlStringList always returns a string array, so it is not re-wrapped.
    $allowed = Get-YamlStringList -Entry $toolsEntry
    $disallowed = Get-YamlStringList -Entry $disallowedEntry
    $restricted = ($allowed.Count -gt 0) -and (-not ($allowed -contains '*')) -and (-not ($allowed -contains 'All tools'))
    $granted = Get-GrantedTools -Allowed $allowed -Disallowed $disallowed -Tools @('Write', 'Edit')

    if ($granted.Count -eq 0) {
        if ($restricted) {
            $details = 'allowed tools: ' + ($allowed -join ', ')
        } else {
            $details = 'tools are unrestricted, disallowedTools: ' + ($disallowed -join ', ')
        }
        Add-CheckResult -Name $toolsCheckName -Passed $true -Details $details
    } else {
        Add-CheckResult -Name $toolsCheckName -Passed $false -Details ('allowed for QA: ' + ($granted -join ', '))
    }
}

function Invoke-QaMemoryDirectoryCheck {
    $relative = '.claude/agent-memory/qa'
    $path = Get-ProjectPath -RelativePath $relative
    if (Test-Path -LiteralPath $path) {
        Add-CheckResult -Name 'QA memory directory absent' -Passed $false -Details ($relative + ' exists')
    } else {
        Add-CheckResult -Name 'QA memory directory absent' -Passed $true -Details ($relative + ' does not exist')
    }
}

function Invoke-GitIgnoreChecks {
    foreach ($expectation in $script:IgnoreExpectations) {
        $expected = 'not ignored'
        if ($expectation.ShouldBeIgnored) {
            $expected = 'ignored'
        }
        $name = 'gitignore: {0} is {1}' -f $expectation.Path, $expected
        if (-not $script:GitPath) {
            Add-CheckResult -Name $name -Passed $false -Details 'git was not found in PATH'
            continue
        }
        $result = Test-GitIgnoredPath -RelativePath $expectation.Path
        if ($result.Error) {
            Add-CheckResult -Name $name -Passed $false -Details $result.Error
            continue
        }
        $actual = 'not ignored'
        if ($result.Ignored) {
            $actual = 'ignored'
        }
        if ($result.Ignored -eq [bool]$expectation.ShouldBeIgnored) {
            Add-CheckResult -Name $name -Passed $true -Details ('git check-ignore: ' + $actual)
        } else {
            Add-CheckResult -Name $name -Passed $false -Details ('git check-ignore reports: ' + $actual)
        }
    }
}

function Invoke-AllChecks {
    Invoke-FileChecks
    Invoke-ToolChecks
    $pythonInfo = Resolve-PythonInterpreter
    Invoke-PythonChecks -PythonInfo $pythonInfo
    Invoke-TomlChecks -PythonInfo $pythonInfo
    Invoke-PowerShellSyntaxChecks
    Invoke-QaAgentChecks
    Invoke-QaMemoryDirectoryCheck
    Invoke-GitIgnoreChecks
    Invoke-StructureCheck
}

function Write-CheckTable {
    param(
        [Parameter(Mandatory = $true)]$Results,
        [int]$Passed,
        [int]$Failed,
        [string]$Status
    )

    $nameWidth = 'Check'.Length
    foreach ($result in $Results) {
        if ($result.Name.Length -gt $nameWidth) {
            $nameWidth = $result.Name.Length
        }
    }
    $statusWidth = 'Status'.Length

    Write-Output 'AI Dev Team local check'
    Write-Output ('Root: ' + $script:ProjectRoot)
    Write-Output ''
    Write-Output ('{0}  {1}  {2}' -f 'Check'.PadRight($nameWidth), 'Status'.PadRight($statusWidth), 'Details')
    Write-Output ('{0}  {1}  {2}' -f ('-' * $nameWidth), ('-' * $statusWidth), ('-' * 7))
    foreach ($result in $Results) {
        Write-Output ('{0}  {1}  {2}' -f $result.Name.PadRight($nameWidth), $result.Status.PadRight($statusWidth), $result.Details)
    }
    Write-Output ''
    Write-Output ('Passed: {0}  Failed: {1}' -f $Passed, $Failed)
    if ($Status -eq 'PASS') {
        Write-Output 'TEAM_CHECK_OK'
    } else {
        Write-Output 'TEAM_CHECK_FAILED'
    }
}

function Write-CheckJson {
    param(
        [Parameter(Mandatory = $true)]$Results,
        [int]$Passed,
        [int]$Failed,
        [string]$Status
    )

    $checks = @()
    foreach ($result in $Results) {
        $checks += [ordered]@{
            name    = [string]$result.Name
            status  = [string]$result.Status
            details = [string]$result.Details
        }
    }
    $payload = [ordered]@{
        status = $Status
        passed = $Passed
        failed = $Failed
        checks = @($checks)
    }
    Write-Output ($payload | ConvertTo-Json -Depth 5)
}

$internalError = $null
try {
    Invoke-AllChecks
} catch {
    $internalError = ConvertTo-SingleLine -Text $_.Exception.Message -MaxLength 200
    Add-CheckResult -Name 'Internal script error' -Passed $false -Details $internalError
}

$failedCount = 0
try {
    $passedCount = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
    $failedCount = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
    $overallStatus = 'FAIL'
    if ($failedCount -eq 0 -and $null -eq $internalError) {
        $overallStatus = 'PASS'
    }

    if ($Json) {
        Write-CheckJson -Results $script:Results -Passed $passedCount -Failed $failedCount -Status $overallStatus
    } else {
        Write-CheckTable -Results $script:Results -Passed $passedCount -Failed $failedCount -Status $overallStatus
    }
} catch {
    [Console]::Error.WriteLine('Internal error while writing the report: ' + $_.Exception.Message)
    exit 2
}

if ($null -ne $internalError) {
    [Console]::Error.WriteLine('Internal error: ' + $internalError)
    exit 2
}
if ($failedCount -gt 0) {
    exit 1
}
exit 0
