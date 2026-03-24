@echo off
setlocal EnableExtensions EnableDelayedExpansion

echo.
echo === Unshackle setup (Windows) ===
echo.

where uv >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] uv is already installed.
    goto install_deps
)

echo [..] uv not found. Installing...

powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if %errorlevel% neq 0 (
    echo [ERR] Failed to install uv.
    echo PowerShell may be blocking scripts. Try:
    echo   Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
    echo or install manually: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

set "UV_BIN="
for %%D in ("%USERPROFILE%\.local\bin" "%LOCALAPPDATA%\Programs\uv\bin" "%USERPROFILE%\.cargo\bin") do (
    if exist "%%~fD\uv.exe" set "UV_BIN=%%~fD"
)

if not defined UV_BIN (
    echo [WARN] Could not locate uv.exe. You may need to reopen your terminal.
) else (
    set "PATH=%UV_BIN%;%PATH%"
)

:: Verify
uv --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERR] uv still not reachable in this shell. Open a new terminal and re-run this script.
    pause
    exit /b 1
)
echo [OK] uv installed and reachable.

:install_deps
echo.
uv sync
if %errorlevel% neq 0 (
    echo [ERR] Dependency install failed. See errors above.
    pause
    exit /b 1
)

echo.
echo Installation completed successfully!
echo Try:
echo   uv run unshackle --help
echo.
pause
endlocal
