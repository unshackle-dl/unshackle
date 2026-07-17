# Run the downloader benchmark suite and save a readable report for comparison.
# Offline synthetic + fault runs by default; -live also hits public test CDNs
# (needs network + ffprobe on PATH). Usually launched via bench_suite.bat.
#   powershell -File scripts\bench_suite.ps1          # offline
#   powershell -File scripts\bench_suite.ps1 --live   # + live/network tests
param([string]$mode = "offline")
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

$live = ($mode -eq "--live" -or $mode -eq "-live" -or $mode -eq "live")
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = "scripts\bench-results\$($env:COMPUTERNAME)_$stamp.txt"
if (-not (Test-Path "scripts\bench-results")) { New-Item -ItemType Directory -Path "scripts\bench-results" | Out-Null }

function Run($a) {
    Write-Output ""
    Write-Output ("==================== python " + ($a -join " ") + " ====================")
    # stringify the merged stream: PS 5.1 wraps native stderr lines in ErrorRecords,
    # which render as NativeCommandError blocks (expected-fail runs looked like crashes)
    & uv run python @a 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Output ("(exit code: " + $LASTEXITCODE + ")")
    }
}

# collect everything, then tee to the report so tables land in the file too
& {
    Write-Output "# unshackle downloader benchmark"
    Write-Output ("host:   " + $env:COMPUTERNAME)
    Write-Output ("date:   " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    Write-Output ("git:    " + (git rev-parse --abbrev-ref HEAD) + " @ " + (git rev-parse --short HEAD))
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Write-Output ("cpu:    " + $cpu.Name + " (" + $cpu.NumberOfCores + "c/" + $cpu.NumberOfLogicalProcessors + "t)")
    $mem = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Output ("mem:    " + $mem + " GiB")
    Write-Output ("os:     " + $os.Caption + " " + $os.Version)
    Write-Output ("python: " + (& uv run python -V 2>&1 | ForEach-Object { "$_" }))
    Write-Output ("mode:   " + $(if ($live) { "live" } else { "offline" }))

    # negotiated link speed per NIC; shows the physical ceiling before the throughput run
    Write-Output ""
    Write-Output "==================== nic link speed ===================="
    Get-NetAdapter | Where-Object Status -eq 'Up' |
        ForEach-Object { Write-Output ("{0}: {1} ({2})" -f $_.Name, $_.LinkSpeed, $_.InterfaceDescription) }

    # link baseline: max line speed, so downloader numbers can be read against it
    # (e.g. "2Gbe -> ~1900 Mbps expected ceiling"). Uses whatever CLI exists.
    Write-Output ""
    Write-Output "==================== speedtest baseline ===================="
    $global:LASTEXITCODE = 0
    if (Get-Command speedtest -ErrorAction SilentlyContinue) {
        & speedtest --accept-license --accept-gdpr 2>&1 | ForEach-Object { "$_" }
    } elseif (Get-Command npx -ErrorAction SilentlyContinue) {
        # Netflix fast.com; download + upload + latency + server, clean when piped
        & npx --yes fast-cli --upload --verbose 2>&1 | ForEach-Object { "$_" }
    } elseif (Get-Command speedtest-cli -ErrorAction SilentlyContinue) {
        & speedtest-cli --simple 2>&1 | ForEach-Object { "$_" }
    } else {
        Write-Output "no speedtest CLI found (install Ookla 'speedtest', have npx, or 'pip install speedtest-cli') - skipping"
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Output ("(speedtest CLI failed - exit code " + $LASTEXITCODE + "; numbers above may be missing)")
    }

    # 1. Synthetic downloader benchmark (offline, deterministic)
    Run @("scripts/bench_downloader.py", "--segments", "64", "--workers", "1,2,4,8,16", "--runs", "3")
    Run @("scripts/bench_downloader.py", "--segments", "64", "--workers", "1,2,4,8,16", "--adaptive")
    Run @("scripts/bench_downloader.py", "--workers", "4,8,16", "--no-read1")

    # 2. Fault paths (--fault-stall is EXPECTED to fail + exit non-zero)
    Run @("scripts/bench_downloader.py", "--fault-503", "2", "--fast-timeouts", "--segments", "8")
    Run @("scripts/bench_downloader.py", "--fault-reset", "2", "--fast-timeouts", "--segments", "8")
    Run @("scripts/bench_downloader.py", "--fault-stall", "1", "--fast-timeouts", "--segments", "8")

    # 3. Multiprocess path (>=24 segments engages spawned children; strided chunk split).
    # big batch: at small sizes the fixed spawn cost drowns the throughput signal
    Run @("scripts/bench_downloader.py", "--segments", "128", "--workers", "8", "--procs", "2")
    Run @("scripts/bench_downloader.py", "--segments", "128", "--workers", "8", "--procs", "4")
    Run @("scripts/bench_downloader.py", "--fault-stall", "1", "--fast-timeouts", "--segments", "32", "--workers", "8", "--procs", "2")

    # 4. Hedge + slow tail (last segments crawl; hedging and striding bound the tail)
    Run @("scripts/bench_downloader.py", "--fault-tail-slow", "4", "--fast-timeouts", "--segments", "32", "--workers", "8")
    Run @("scripts/bench_downloader.py", "--fault-tail-slow", "4", "--fast-timeouts", "--segments", "32", "--workers", "8", "--procs", "4")
    Run @("scripts/bench_downloader.py", "--fault-tail-slow", "2", "--fast-timeouts", "--adaptive", "--segments", "16", "--workers", "8")

    # 5. rnet session path (loopback through the TLS-fingerprint client's own read loop)
    Run @("scripts/bench_downloader.py", "--segments", "32", "--workers", "8", "--impersonate", "Chrome131")

    # 6. Leak regression tests (locked handles / leaked workers; unlink checks bite on Windows)
    Write-Output ""
    Write-Output "==================== pytest tests/test_downloader_leaks.py ===================="
    & uv run pytest tests/test_downloader_leaks.py -q 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) {
        Write-Output "(pytest failed or missing - run 'uv sync' to install dev deps)"
    }

    # 7. Live network tests (opt-in)
    if ($live) {
        Run @("scripts/live_manifest_test.py", "--workers", "16", "--track", "largest")
        Run @("scripts/live_segmentbase_ab.py", "--track", "largest")
        Run @("scripts/bench_downloader.py", "--urls-file", "scripts/bench-urls/apple_sustained_4x585mb.txt", "--impersonate", "Chrome131")
        Run @("scripts/bench_downloader.py", "--urls-file", "scripts/bench-urls/akamai_4k_segments.txt", "--impersonate", "Chrome131")
    }

    Write-Output ""
    Write-Output ("report saved: " + $out)
} | Tee-Object -FilePath $out
# progressive tee keeps a partial report if the run dies; rewrite as utf8 at the end
# (Tee-Object only writes utf16) so reports diff and grep cleanly across machines
(Get-Content $out) | Out-File -FilePath $out -Encoding utf8
