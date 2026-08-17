@echo off
REM Run the downloader benchmark suite and save a readable report for comparison.
REM Offline synthetic + fault runs by default; --live also hits public test CDNs
REM (needs network + ffprobe on PATH).
REM   scripts\bench_suite.bat          (offline suite)
REM   scripts\bench_suite.bat --live   (+ live/network tests)
REM Output: scripts\bench-results\<host>_<timestamp>.txt
setlocal
cd /d "%~dp0.."
if not exist "scripts\bench-results" mkdir "scripts\bench-results"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bench_suite.ps1" %*
endlocal
