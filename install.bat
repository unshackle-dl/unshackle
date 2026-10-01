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
findstr /b /c:"name = \"unshackle\"" "%~dp0pyproject.toml" >nul 2>&1
if %errorlevel% neq 0 goto tool_install

cd /d "%~dp0"
uv sync --compile-bytecode
if %errorlevel% neq 0 (
    echo [ERR] Dependency install failed. See errors above.
    pause
    exit /b 1
)
set "SETUP=uv run unshackle setup"
set "RERUN=Run it again with: uv run unshackle setup"
set "NOTE="
goto run_setup

:tool_install
uv tool install --compile-bytecode git+https://github.com/unshackle-dl/unshackle.git
if %errorlevel% neq 0 (
    echo [ERR] unshackle install failed. See errors above.
    pause
    exit /b 1
)
uv tool update-shell
set "TOOL_BIN="
for /f "delims=" %%B in ('uv tool dir --bin') do set "TOOL_BIN=%%B"
set "SETUP="%TOOL_BIN%\unshackle.exe" setup"
set "RERUN=Run it again in a new terminal with: unshackle setup"
set "NOTE=Open a new terminal so that the unshackle command is on your PATH."

:run_setup
echo.
%SETUP%
if %errorlevel% neq 0 (
    echo [ERR] unshackle is installed, but setup failed. See errors above.
    echo %RERUN%
    pause
    exit /b 1
)

echo.
echo Installation completed successfully.
if defined NOTE echo %NOTE%
echo.
pause
endlocal
