#!/usr/bin/env bash
# Run the downloader benchmark suite and save a readable report for comparison.
# Offline synthetic + fault runs by default; --live also hits public test CDNs
# (needs network + ffprobe on PATH).
#   ./scripts/bench_suite.sh          # offline suite
#   ./scripts/bench_suite.sh --live   # + live/network tests
# Output: scripts/bench-results/<host>_<utc-timestamp>.txt
set -uo pipefail
cd "$(dirname "$0")/.."

OUT_DIR="scripts/bench-results"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$OUT_DIR/$(hostname)_${STAMP}.txt"

# tee everything (stdout+stderr) to the report so tables land in the file too
exec > >(tee "$OUT") 2>&1

echo "# unshackle downloader benchmark"
echo "host:   $(hostname)"
echo "date:   $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
echo "git:    $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
echo "cpu:    $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//') ($(nproc) threads)"
echo "mem:    $(free -h --si 2>/dev/null | awk '/^Mem:/{print $2}')"
echo "os:     $(uname -sr)"
echo "python: $(uv run python -V 2>&1)"
echo "mode:   ${1:-offline}"

run() { echo; echo "==================== $* ===================="; uv run python "$@"; }

# link baseline: establishes the max line speed so downloader numbers can be read
# against it (e.g. "2Gbe -> ~1900 Mbps expected ceiling"). Uses whatever CLI exists.
# negotiated link speed per NIC (from sysfs); shows the physical ceiling, e.g. 2500 = 2.5GbE
nic_link_speed() {
  echo; echo "==================== nic link speed ===================="
  local found=0
  for d in /sys/class/net/*; do
    local n state spd
    n=$(basename "$d")
    [ "$n" = "lo" ] && continue
    state=$(cat "$d/operstate" 2>/dev/null)
    [ "$state" = "up" ] || continue
    spd=$(cat "$d/speed" 2>/dev/null)
    echo "$n: ${spd:-?} Mbps"
    found=1
  done
  [ "$found" = 1 ] || echo "no up interfaces with readable speed (virtual/WSL/wifi report -1)"
}
nic_link_speed

speedtest_baseline() {
  echo; echo "==================== speedtest baseline ===================="
  if command -v speedtest >/dev/null 2>&1; then
    speedtest --accept-license --accept-gdpr 2>&1 || echo "speedtest failed"
  elif command -v npx >/dev/null 2>&1; then
    # Netflix fast.com; download + upload + latency + server, clean when piped
    npx --yes fast-cli --upload --verbose 2>&1 || echo "npx fast-cli failed"
  elif command -v speedtest-cli >/dev/null 2>&1; then
    speedtest-cli --simple 2>&1 || echo "speedtest-cli failed"
  else
    echo "no speedtest CLI found (install Ookla 'speedtest', have npx, or 'pipx install speedtest-cli') - skipping"
  fi
}
speedtest_baseline

# 1. Synthetic downloader benchmark (offline, deterministic)
run scripts/bench_downloader.py --segments 64 --workers 1,2,4,8,16 --runs 3
run scripts/bench_downloader.py --segments 64 --workers 1,2,4,8,16 --adaptive
run scripts/bench_downloader.py --workers 4,8,16 --no-read1

# 2. Fault paths (fast timeouts; --fault-stall is EXPECTED to fail + exit non-zero)
run scripts/bench_downloader.py --fault-503 2 --fast-timeouts --segments 8
run scripts/bench_downloader.py --fault-reset 2 --fast-timeouts --segments 8
run scripts/bench_downloader.py --fault-stall 1 --fast-timeouts --segments 8 || true

# 2b. Fault-injection reliability + request amplification (fault_server.py; every run is
# byte-integrity verified, so a CORRUPTION line here is a real finalize-wrong-bytes bug).
# loopback is the zero-fault python-overhead ceiling; the rest exercise retry/hedge/tail paths.
run scripts/bench_downloader.py --fault-profile loopback --segments 64 --workers 8,16 --runs 3
run scripts/bench_downloader.py --fault-profile rate-limit --segments 32 --workers 8
run scripts/bench_downloader.py --fault-profile flaky-first --segments 16 --workers 8 --fast-timeouts
run scripts/bench_downloader.py --fault-profile reset --segments 16 --workers 8 --fast-timeouts
run scripts/bench_downloader.py --fault-profile stall --segments 16 --workers 8 --fast-timeouts
run scripts/bench_downloader.py --fault-profile tail-slow --segments 32 --workers 8 --adaptive --fast-timeouts

# 3. Multiprocess path (>=24 segments engages spawned children; strided chunk split).
# big batch: at small sizes the fixed spawn cost drowns the throughput signal
run scripts/bench_downloader.py --segments 128 --workers 8 --procs 2
run scripts/bench_downloader.py --segments 128 --workers 8 --procs 4
run scripts/bench_downloader.py --fault-stall 1 --fast-timeouts --segments 32 --workers 8 --procs 2 || true

# 4. Hedge + slow tail (last segments crawl; hedging and striding bound the tail)
run scripts/bench_downloader.py --fault-tail-slow 4 --fast-timeouts --segments 32 --workers 8
run scripts/bench_downloader.py --fault-tail-slow 4 --fast-timeouts --segments 32 --workers 8 --procs 4
run scripts/bench_downloader.py --fault-tail-slow 2 --fast-timeouts --adaptive --segments 16 --workers 8

# 5. rnet session path (loopback through the TLS-fingerprint client's own read loop)
run scripts/bench_downloader.py --segments 32 --workers 8 --impersonate Chrome131 || true

# 6. Leak regression tests (locked handles / leaked workers; unlink checks bite on Windows)
echo; echo "==================== pytest tests/test_downloader_leaks.py ===================="
uv run pytest tests/test_downloader_leaks.py -q || echo "(pytest failed or missing - run 'uv sync' to install dev deps)"

# 6b. Low-core variant: pin to two cores to emulate a small box, so the python-overhead
# ceiling and one fault profile can be read against the full-core numbers above. taskset is
# util-linux (Linux only); skipped cleanly elsewhere.
if command -v taskset >/dev/null 2>&1 && taskset -c 0-1 true 2>/dev/null; then
  echo; echo "==================== low-core variant (taskset -c 0-1, 2 cores) ===================="
  run_lowcore() { echo; echo "-------------------- taskset -c 0-1 $* --------------------"; taskset -c 0-1 uv run python "$@"; }
  run_lowcore scripts/bench_downloader.py --fault-profile loopback --segments 64 --workers 8,16 --runs 3
  run_lowcore scripts/bench_downloader.py --fault-profile rate-limit --segments 32 --workers 8
else
  echo; echo "==================== low-core variant skipped (taskset missing or CPUs 0-1 not schedulable) ===================="
fi

# 7. Live network tests (opt-in)
if [[ "${1:-}" == "--live" ]]; then
  run scripts/live_manifest_test.py --workers 16 --track largest
  run scripts/live_segmentbase_ab.py --track largest
  run scripts/bench_downloader.py --urls-file scripts/bench-urls/apple_sustained_4x585mb.txt --impersonate Chrome131
  run scripts/bench_downloader.py --urls-file scripts/bench-urls/akamai_4k_segments.txt --impersonate Chrome131
fi

echo; echo "report saved: $OUT"
