param(
    [string]$Python = "python",
    [string]$OutputDirectory = (Split-Path -Parent $PSScriptRoot),
    [string]$AppCredentialsPath = ""
)

$ErrorActionPreference = "Stop"
$PythonExecutable = (Get-Command $Python -CommandType Application -ErrorAction Stop).Source
# Check the selected interpreter before touching an existing executable. The
# Windows/Python wheel must load and Telethon must actually select its native
# implementation, rather than silently falling back to pure Python AES.
& $PythonExecutable -B -X utf8 (Join-Path $PSScriptRoot "tgmover\acceleration.py") --require-cryptg --benchmark --size-bytes 16384 --repeats 1
if ($LASTEXITCODE -ne 0) {
    throw "cryptg native AES is unavailable. Install requirements-build.txt using the selected Python interpreter before building."
}
$BuildRoot = Join-Path $PSScriptRoot ".build"
$ExistingExe = Join-Path $OutputDirectory "TelegramMover.exe"
$PreviousExe = Join-Path $BuildRoot "backups\TelegramMover-console.exe"
if ((Test-Path -LiteralPath $ExistingExe) -and -not (Test-Path -LiteralPath $PreviousExe)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $PreviousExe) | Out-Null
    Copy-Item -LiteralPath $ExistingExe -Destination $PreviousExe
}
$BuildArguments = @(
    "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--noupx",
    "--name", "TelegramMover", "--collect-all", "telethon",
    "--collect-all", "cryptg", "--hidden-import", "tgmover.acceleration",
    "--add-data", "$(Join-Path $PSScriptRoot 'config.example.toml');.",
    "--distpath", $OutputDirectory,
    "--workpath", (Join-Path $BuildRoot "gui-work"),
    "--specpath", $BuildRoot,
    (Join-Path $PSScriptRoot "gui_main.py")
)
if ($AppCredentialsPath) {
    $ResolvedAppCredentials = (Resolve-Path -LiteralPath $AppCredentialsPath -ErrorAction Stop).Path
    $CredentialsStage = Join-Path $BuildRoot "app-credentials"
    New-Item -ItemType Directory -Force -Path $CredentialsStage | Out-Null
    $StagedCredentials = Join-Path $CredentialsStage "app_credentials.toml"
    & $PythonExecutable -X utf8 (Join-Path $PSScriptRoot "tgmover\credentials.py") --validate-file $ResolvedAppCredentials --stage-file $StagedCredentials
    if ($LASTEXITCODE -ne 0) {
        throw "Invalid application credentials. No executable was replaced."
    }
    $BuildArguments = $BuildArguments[0..($BuildArguments.Length - 2)] + @("--add-data", "$StagedCredentials;.", $BuildArguments[-1])
}
$OriginalBuildPath = $env:PATH
try {
    # Qt depends on the Windows ICU libraries. Unrelated tools on PATH (such as
    # Poppler) can supply an incompatible icuuc.dll with the same filename.
    # Search only the selected Python and Windows while collecting binaries.
    $env:PATH = @(
        (Join-Path $env:SystemRoot "System32"),
        $env:SystemRoot,
        (Split-Path -Parent $PythonExecutable)
    ) -join ";"
    & $PythonExecutable @BuildArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Windows executable build failed."
    }
} finally {
    $env:PATH = $OriginalBuildPath
}

$ConfigDestination = Join-Path $OutputDirectory "config.toml"
if (-not (Test-Path -LiteralPath $ConfigDestination)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "config.example.toml") -Destination $ConfigDestination
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "config.example.toml") -Destination (Join-Path $OutputDirectory "config.example.toml")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "WINDOWS-README.txt") -Destination (Join-Path $OutputDirectory "使用说明.txt")
Write-Output "Ready: $(Join-Path $OutputDirectory 'TelegramMover.exe')"
