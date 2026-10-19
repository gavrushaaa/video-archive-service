$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$BuildPython = Join-Path $ProjectRoot ".venv-desktop\Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    python -m venv .venv-desktop
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось создать виртуальное окружение для сборки."
    }
}

& $BuildPython -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось установить зависимости сборки."
}

$OriginalPath = $env:PATH
$WindowsSystemDirectory = [System.IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32")).TrimEnd('\')
$BuildPath = foreach ($Entry in ($OriginalPath -split ';')) {
    if ([string]::IsNullOrWhiteSpace($Entry)) {
        continue
    }
    $Directory = $Entry.Trim('"')
    try {
        $Directory = [System.IO.Path]::GetFullPath($Directory).TrimEnd('\')
    } catch {
        # Keep unusual PATH entries; they cannot shadow a DLL through this check.
    }
    if ($Directory -ine $WindowsSystemDirectory -and (Test-Path -LiteralPath (Join-Path $Directory "icuuc.dll"))) {
        Write-Host "Исключаю сторонний ICU из PATH сборки: $Directory"
        continue
    }
    $Entry
}

try {
    $env:PATH = $BuildPath -join ';'
    & $BuildPython -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onefile `
        --name VideoArchive `
        --distpath output/windows `
        --workpath build/windows `
        --specpath build/windows `
        --add-data "$ProjectRoot\static;static" `
        desktop_app.py
    if ($LASTEXITCODE -ne 0) {
        throw "Сборка приложения завершилась с ошибкой."
    }
} finally {
    $env:PATH = $OriginalPath
}

Write-Host "Готово: $ProjectRoot\output\windows\VideoArchive.exe"
