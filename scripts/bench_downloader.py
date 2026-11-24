#!/usr/bin/env python3
"""
Benchmark the unified downloader (``unshackle.core.downloaders.requests.requests``)
throughput against worker count, to baseline the current downloader and later
validate an adaptive worker controller.

Default (synthetic) mode spins up a loopback HTTP server serving deterministic
segments, feeds them through the real download code path, and reports
workers -> seconds -> MB/s plus a tail-droop metric. Optional response shaping
(``--latency``, ``--rate``) and fault injection (``--fault-*``) approximate a
misbehaving CDN. ``--urls-file`` swaps in real URLs (only mode that hits the
network; fault flags are ignored there).

Fault modes (synthetic only) exercise the downloader's timeout / retry / hedge
paths. Because the real read timeout is 30s, pair them with ``--fast-timeouts``
(bench-only monkeypatch of READ_TIMEOUT/RETRY_WAIT/HEDGE_MIN_WAIT to a few
seconds) so fault runs finish quickly. A ``--fault-stall`` run is expected to
FAIL its completeness check (the stall never resolves) and exit non-zero.

Usage:
    uv run python scripts/bench_downloader.py --segments 64 --workers 1,2,4,8,16
    uv run python scripts/bench_downloader.py --rate 2048 --latency 50
    uv run python scripts/bench_downloader.py --fault-503 2 --fast-timeouts --segments 8
    uv run python scripts/bench_downloader.py --urls-file urls.txt --impersonate Chrome131
"""

from __future__ import annotations

import http.server
import importlib
import math
import multiprocessing
import os
import socket
import socketserver
import statistics
import struct
import tempfile
import threading
import time
from pathlib import Path
from shutil import rmtree
from typing import Any, Optional

import click
import fault_server
from rich.console import Console
from rich.table import Table

from unshackle.core.config import config
from unshackle.core.constants import DOWNLOAD_CANCELLED
from unshackle.core.downloaders.requests import requests as download_batch

dl_mod = importlib.import_module("unshackle.core.downloaders.requests")

MIB = 1024 * 1024
TAIL_FRACTION = 0.25
TAIL_SLOW_KIB = 256
SLOW_CONN_KIB = 2048
console = Console()


class SegmentServer(socketserver.ThreadingTCPServer):
    """Loopback server serving a shared synthetic buffer with optional shaping + faults."""

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self, buffer: bytes, latency_ms: int, rate_kib: int, faults: dict[str, int], total_segments: int
    ) -> None:
        self.buffer = buffer
        self.latency_ms = latency_ms
        self.rate_kib = rate_kib
        self.faults = faults
        self.total_segments = total_segments
        self.attempts: dict[int, int] = {}
        self.connections = 0
        self.lock = threading.Lock()
        self.stall_event = threading.Event()
        super().__init__(("127.0.0.1", 0), SegmentHandler)

    def reset(self) -> None:
        with self.lock:
            self.attempts.clear()
            self.connections = 0

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class SegmentHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        server: SegmentServer = self.server  # type: ignore[assignment]
        with server.lock:
            server.connections += 1
            self.slow_conn = server.connections <= server.faults["slow_conn"]

    def _parse_range(self, total: int) -> Optional[tuple[int, int]]:
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        start_s, _, end_s = header[len("bytes=") :].partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else total - 1
        return start, min(end, total - 1)

    def do_GET(self) -> None:
        server: SegmentServer = self.server  # type: ignore[assignment]
        if not self.path.startswith("/seg/"):
            self.send_error(404)
            return
        try:
            idx = int(self.path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_error(404)
            return

        with server.lock:
            server.attempts[idx] = server.attempts.get(idx, 0) + 1
            attempt = server.attempts[idx]

        f = server.faults
        if idx < f["stall"]:
            self._serve(stall=True)
            return
        if attempt == 1 and idx < f["reset"]:
            self._serve(reset=True)
            return
        if attempt == 1 and idx < f["http503"]:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rate = server.rate_kib
        if f["tail_slow"] and idx >= server.total_segments - f["tail_slow"]:
            rate = TAIL_SLOW_KIB
        if f["tail_slow_once"] and attempt == 1 and idx >= server.total_segments - f["tail_slow_once"]:
            rate = TAIL_SLOW_KIB
        if self.slow_conn:
            rate = SLOW_CONN_KIB
        self._serve(rate=rate)

    def _serve(self, rate: int = 0, stall: bool = False, reset: bool = False) -> None:
        server: SegmentServer = self.server  # type: ignore[assignment]
        full = server.buffer
        rng = self._parse_range(len(full))
        if rng is not None and rng[0] >= len(full):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(full)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng is not None:
            start, end = rng
            body = full[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(full)}")
        else:
            start = 0
            body = full
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        if server.latency_ms:
            time.sleep(server.latency_ms / 1000)
        try:
            if stall:
                stall_point = len(full) // 2
                chunk = full[start:stall_point]
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                server.stall_event.wait(120)
            elif reset:
                half = max(1, len(body) // 2)
                self.wfile.write(body[:half])
                self.wfile.flush()
                sock = self.connection
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sock.close()
                self.close_connection = True
            else:
                self._write_body(body, rate)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _write_body(self, body: bytes, rate_kib: int) -> None:
        if rate_kib <= 0:
            self.wfile.write(body)
            return
        chunk = 64 * 1024
        per_chunk = chunk / (rate_kib * 1024)
        for off in range(0, len(body), chunk):
            self.wfile.write(body[off : off + chunk])
            time.sleep(per_chunk)


def run_batch(
    urls: list[str], out_dir: Path, workers: int, session: Optional[Any], adaptive: bool = False, procs: int = 1
) -> tuple[float, list[float], Optional[BaseException]]:
    """Download one batch through the real code path.

    Returns (wall seconds, completion timestamps relative to start, error-or-None).
    Clears the process-global cancel flag first so a prior failed run can't poison this one.
    """
    DOWNLOAD_CANCELLED.clear()
    completions: list[float] = []
    err: Optional[BaseException] = None
    start = time.perf_counter()
    try:
        for event in download_batch(
            urls=urls,
            output_dir=out_dir,
            filename="seg_{i}.bin",
            max_workers=workers,
            session=session,
            **({"adaptive": True} if adaptive else {}),
            **({"processes": procs} if procs > 1 else {}),
        ):
            if "file_downloaded" in event:
                completions.append(time.perf_counter() - start)
    except BaseException as exc:  # noqa: BLE001 - bench records failure, keeps sweeping
        err = exc
    wall = time.perf_counter() - start
    if err is not None:
        deadline = time.monotonic() + dl_mod.READ_TIMEOUT + dl_mod.RETRY_WAIT + 2
        while time.monotonic() < deadline and (
            any(t.name.startswith("ThreadPoolExecutor") for t in threading.enumerate())
            or multiprocessing.active_children()
        ):
            time.sleep(0.1)
    strays, locked = sweep_strays(out_dir)
    pool_threads = sum(1 for t in threading.enumerate() if t.name.startswith("ThreadPoolExecutor"))
    return wall, completions, err, strays, locked, pool_threads


def incomplete_segments(out_dir: Path, expected: int, expected_bytes: Optional[bytes]) -> list[int]:
    """Segment indexes that are missing, empty, or (synthetic mode) not byte-identical.

    Content compare (not just size) is required: on a mid-body interruption the downloader
    can finalize a full-size but zero-padded segment (the tmp is pre-truncated to content_length
    and the exception path skips the truncate-back), so only a byte compare catches it. Real-URL
    mode has no reference bytes, so it falls back to a nonzero check.
    """
    bad: list[int] = []
    for i in range(expected):
        p = out_dir / f"seg_{i}.bin"
        if not p.exists():
            bad.append(i)
            continue
        if expected_bytes is not None:
            if p.read_bytes() != expected_bytes:
                bad.append(i)
        elif p.stat().st_size == 0:
            bad.append(i)
    return bad


def downloaded_mib(out_dir: Path) -> float:
    return sum(f.stat().st_size for f in out_dir.glob("seg_*.bin")) / MIB


def sweep_strays(out_dir: Path) -> tuple[int, int]:
    """Emulate the manifest merge's stray sweep (glob('*.!dev') unlink) at batch completion.

    Returns (strays, locked). locked > 0 is the WinError 32 crash: a superseded hedge
    racer still holds its file handle when merge deletes strays. POSIX deletes open
    files silently, so there /proc/self/fd is checked to catch the same leak.
    """
    strays = list(out_dir.glob("*.!dev"))
    locked = 0
    fd_dir = Path("/proc/self/fd")
    if fd_dir.exists():
        open_targets = set()
        for fd in fd_dir.iterdir():
            try:
                open_targets.add(os.readlink(fd))
            except OSError:
                continue
        locked = sum(1 for s in strays if str(s) in open_targets)
    for s in strays:
        try:
            s.unlink()
        except OSError:
            locked += 1
    return len(strays), locked


def tail_droop(completions: list[float], wall: float) -> tuple[int, float, float]:
    """Return (bucket size, seconds the last TAIL_FRACTION took, that as % of wall)."""
    n = len(completions)
    if n == 0 or wall <= 0:
        return 0, 0.0, 0.0
    c = sorted(completions)
    bucket = max(1, math.ceil(n * TAIL_FRACTION))
    boundary = c[n - bucket - 1] if n - bucket - 1 >= 0 else 0.0
    tail_time = wall - boundary
    return bucket, tail_time, tail_time / wall * 100


Row = tuple[int, float, float, int, float, float, list[int], bool, int, int, int]


def sweep(
    urls: list[str],
    workers: list[int],
    runs: int,
    session: Optional[Any],
    server: Optional[SegmentServer],
    expected_bytes: Optional[bytes],
    adaptive: bool = False,
    procs: int = 1,
) -> list[Row]:
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="dlbench_") as root_str:
        root = Path(root_str)
        for w in workers:
            samples: list[tuple[float, list[float], float, list[int], bool]] = []
            for _ in range(runs):
                if server is not None:
                    server.reset()
                out_dir = Path(tempfile.mkdtemp(prefix="run_", dir=root))
                secs, comps, err, strays, locked, pool_threads = run_batch(urls, out_dir, w, session, adaptive, procs)
                bad = incomplete_segments(out_dir, len(urls), expected_bytes)
                samples.append(
                    (secs, comps, downloaded_mib(out_dir), bad, err is not None, strays, locked, pool_threads)
                )
                rmtree(out_dir, ignore_errors=True)
            median_s = statistics.median(s[0] for s in samples)
            best = min(samples, key=lambda s: abs(s[0] - median_s))
            bucket, tail_s, tail_pct = tail_droop(best[1], best[0])
            mbps = best[2] / best[0] if best[0] else 0.0
            rows.append(
                (
                    w,
                    median_s,
                    mbps,
                    bucket,
                    tail_s,
                    tail_pct,
                    best[3],
                    best[4],
                    max(s[5] for s in samples),
                    max(s[6] for s in samples),
                    max(s[7] for s in samples),
                )
            )
    return rows


def print_report(rows: list[Row], runs: int, total_segments: int) -> None:
    table = Table(title=f"downloader throughput (median of {runs} run{'s' if runs != 1 else ''})")
    table.add_column("workers", justify="right")
    table.add_column("seconds", justify="right")
    table.add_column("MB/s", justify="right")
    table.add_column("status", justify="left")
    table.add_column("sweep", justify="left")
    for w, secs, mbps, _bucket, _ts, _tp, missing, raised, strays, locked, threads in rows:
        if missing:
            status = f"[red]FAIL {len(missing)} incomplete[/]"
        elif raised:
            status = "[yellow]raised (all present)[/]"
        else:
            status = "[green]ok[/]"
        if locked:
            sweep_status = f"[red]{locked} LOCKED (WinError 32)[/]"
        elif threads:
            sweep_status = f"[yellow]{threads} pool threads live[/]"
        elif strays:
            sweep_status = f"{strays} strays"
        else:
            sweep_status = "[green]clean[/]"
        table.add_row(str(w), f"{secs:.3f}", f"{mbps:.1f}", status, sweep_status)
    console.print(table)
    for w, secs, _mbps, bucket, tail_s, tail_pct, *_rest in rows:
        console.print(
            f"tail-droop  workers={w:<3} wall={secs:.3f}s  "
            f"last {int(TAIL_FRACTION * 100)}% ({bucket} of {total_segments} segs) "
            f"took {tail_pct:.1f}% of wall ({tail_s:.3f}s)"
        )


def fault_integrity(out_dir: Path, segments: int, seg_size: int) -> tuple[list[int], list[int]]:
    """Verify every output file byte-for-byte against its seeded expected payload.

    Returns (missing, corrupt). ``corrupt`` (present but not byte-identical) is the loud
    CORRUPTION signal: a fault path that finalizes wrong bytes must fail the run, not pass.
    """
    missing: list[int] = []
    corrupt: list[int] = []
    for i in range(segments):
        p = out_dir / f"seg_{i}.bin"
        if not p.exists():
            missing.append(i)
            continue
        if p.read_bytes() != fault_server.segment_payload(f"/seg/{i}", seg_size):
            corrupt.append(i)
    return missing, corrupt


FaultRow = tuple[int, float, float, list[int], list[int], bool, int, float, int, dict[int, int], int, int, int]


def print_fault_report(rows: list[FaultRow], runs: int, segments: int, profile: str) -> None:
    plural = "s" if runs != 1 else ""
    table = Table(title=f"fault profile '{profile}': {segments} segments (median of {runs} run{plural})")
    table.add_column("workers", justify="right")
    table.add_column("seconds", justify="right")
    table.add_column("MB/s", justify="right")
    table.add_column("status", justify="left")
    table.add_column("requests", justify="right")
    table.add_column("amp", justify="right")
    table.add_column("sweep", justify="left")
    for w, secs, mbps, missing, corrupt, raised, requests_seen, amp, _retries, _codes, strays, locked, threads in rows:
        if corrupt:
            status = f"[red]CORRUPTION {len(corrupt)} segs[/]"
        elif missing:
            status = f"[red]FAIL {len(missing)} incomplete[/]"
        elif raised:
            status = "[yellow]raised (all present)[/]"
        else:
            status = "[green]ok[/]"
        if locked:
            sweep_status = f"[red]{locked} LOCKED (WinError 32)[/]"
        elif threads:
            sweep_status = f"[yellow]{threads} pool threads live[/]"
        elif strays:
            sweep_status = f"{strays} strays"
        else:
            sweep_status = "[green]clean[/]"
        table.add_row(str(w), f"{secs:.3f}", f"{mbps:.1f}", status, str(requests_seen), f"{amp:.2f}x", sweep_status)
    console.print(table)
    for w, _secs, _mbps, _missing, _corrupt, _raised, requests_seen, amp, retries, codes, *_rest in rows:
        code_str = ", ".join(f"{c}:{n}" for c, n in codes.items()) or "none"
        console.print(
            f"amplification  workers={w:<3} {requests_seen} requests for {segments} segments "
            f"({amp:.2f}x, {retries} excess requests (retries/hedges/probes/parts)), statuses [{code_str}]"
        )


def _procs_notice(procs: int, n_urls: int, session: Optional[Any]) -> None:
    """Warn loudly when --procs cannot engage, so a run is never mistaken for a multiprocess test."""
    if procs <= 1:
        return
    if n_urls < dl_mod.MP_MIN_SEGMENTS:
        console.print(
            f"[yellow]--procs {procs} will NOT engage: {n_urls} segments < MP_MIN_SEGMENTS "
            f"({dl_mod.MP_MIN_SEGMENTS}); the single-process path runs instead[/]"
        )
    elif dl_mod.build_session_spec(session) is None:
        console.print(
            f"[yellow]--procs {procs} will NOT engage: session not rebuildable in child processes; "
            f"the single-process path runs instead[/]"
        )


def run_fault_mode(
    profile_name: str,
    segments: int,
    seg_size: int,
    worker_counts: list[int],
    runs: int,
    session: Optional[Any],
    adaptive: bool,
    procs: int,
    seed: int,
) -> None:
    """Start a FaultServer in-process, sweep worker counts, and report reliability + overhead.

    Reuses run_batch/tail_droop/sweep_strays (via run_batch) and the report conventions, adding
    request amplification and byte-exact integrity, the metrics that catch retry/hedge storms
    and silent corruption against a misbehaving host.
    """
    profile = fault_server.resolve_profile(profile_name)
    server = fault_server.FaultServer(profile, seg_size, segments, seed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = str(server.server_address[0]), int(server.server_address[1])
    urls = [f"http://{host}:{port}/seg/{i}" for i in range(segments)]
    console.print(
        f"[cyan]Fault mode:[/] profile={profile_name} seed={seed} "
        f"{segments} x {seg_size / MIB:.2f} MiB, impersonate={getattr(session, 'impersonate_name', None) or 'none'}"
    )

    rows: list[FaultRow] = []
    try:
        with tempfile.TemporaryDirectory(prefix="faultbench_") as root_str:
            root = Path(root_str)
            for w in worker_counts:
                samples: list[tuple[float, float, list[int], list[int], bool, int, dict[int, int], int, int, int]] = []
                for _ in range(runs):
                    server.reset_counters()
                    out_dir = Path(tempfile.mkdtemp(prefix="run_", dir=root))
                    secs, _comps, err, strays, locked, pool_threads = run_batch(
                        urls, out_dir, w, session, adaptive, procs
                    )
                    missing, corrupt = fault_integrity(out_dir, segments, seg_size)
                    stats = server.stats()
                    samples.append(
                        (
                            secs,
                            downloaded_mib(out_dir),
                            missing,
                            corrupt,
                            err is not None,
                            stats["requests"],
                            stats["status_counts"],
                            strays,
                            locked,
                            pool_threads,
                        )
                    )
                    rmtree(out_dir, ignore_errors=True)
                median_s = statistics.median(s[0] for s in samples)
                best = min(samples, key=lambda s: abs(s[0] - median_s))
                mbps = best[1] / best[0] if best[0] else 0.0
                requests_seen = best[5]
                amp = requests_seen / segments if segments else 0.0
                retries = max(0, requests_seen - segments)
                rows.append(
                    (
                        w,
                        median_s,
                        mbps,
                        best[2],
                        best[3],
                        best[4],
                        requests_seen,
                        amp,
                        retries,
                        best[6],
                        max(s[7] for s in samples),
                        max(s[8] for s in samples),
                        max(s[9] for s in samples),
                    )
                )
    finally:
        server.stall_event.set()
        server.shutdown()

    print_fault_report(rows, runs, segments, profile_name)

    broken = {row[0]: (row[3], row[4]) for row in rows if row[3] or row[4]}
    if broken:
        detail = "; ".join(f"workers={w}: missing {miss} corrupt {corr}" for w, (miss, corr) in broken.items())
        raise click.ClickException(f"incomplete/corrupt downloads under fault profile: {detail}")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--segments", default=64, show_default=True, help="Synthetic segment count.")
@click.option("--seg-size", default=2 * MIB, show_default=True, help="Bytes per synthetic segment.")
@click.option("--workers", default="1,2,4,8,16", show_default=True, help="Comma list of max_workers to sweep.")
@click.option("--runs", default=1, show_default=True, help="Runs per worker count; median reported.")
@click.option(
    "--latency", "latency_ms", default=0, show_default=True, help="Per-response delay before first byte (ms)."
)
@click.option(
    "--rate", "rate_kib", default=0, show_default=True, help="Per-response throttle in KiB/s (0 = unthrottled)."
)
@click.option("--fault-stall", default=0, show_default=True, help="First N segments stall forever mid-body.")
@click.option("--fault-reset", default=0, show_default=True, help="First N segments RST mid-body on attempt 1.")
@click.option(
    "--fault-503", "fault_503", default=0, show_default=True, help="First N segments return 503 on attempt 1."
)
@click.option(
    "--fault-tail-slow", default=0, show_default=True, help=f"Last N segments throttled to {TAIL_SLOW_KIB} KiB/s."
)
@click.option(
    "--fault-tail-slow-once",
    default=0,
    show_default=True,
    help=f"Last N segments throttled to {TAIL_SLOW_KIB} KiB/s on attempt 1 only; a hedge/retry runs full speed.",
)
@click.option(
    "--fault-slow-conn",
    default=0,
    show_default=True,
    help=f"First N accepted connections are throttled to {SLOW_CONN_KIB} KiB/s for their whole life.",
)
@click.option(
    "--part-size-mib",
    type=int,
    default=0,
    show_default=True,
    help="Bench-only: override RANGE_PARALLEL_PART_SIZE (MiB); a very large value gives one part per worker.",
)
@click.option(
    "--fast-timeouts",
    is_flag=True,
    help="Bench-only: shrink READ_TIMEOUT/RETRY_WAIT/HEDGE_MIN_WAIT so fault runs finish fast.",
)
@click.option(
    "--urls-file",
    type=click.Path(exists=True, dir_okay=False),
    help="Real URLs, one per line (network mode; faults ignored).",
)
@click.option("--impersonate", help="rnet browser preset (e.g. Chrome131); uses RnetSession in either mode.")
@click.option("--adaptive", is_flag=True, help="Use the adaptive worker controller (max_workers becomes the cap).")
@click.option("--procs", default=1, show_default=True, help="Fan segments across N processes (multiprocess path).")
@click.option(
    "--no-read1", is_flag=True, help="A/B: force racers back to blocking full-chunk reads (pre-fix behavior)."
)
@click.option(
    "--fault-profile",
    type=click.Choice(sorted(fault_server.PROFILES)),
    help="Reliability mode: serve via the fault-injection server (fault_server.py) and verify integrity + amplification.",
)
@click.option("--seed", default=fault_server.DEFAULT_SEED, show_default=True, help="Fault RNG seed (--fault-profile).")
def main(
    segments: int,
    seg_size: int,
    workers: str,
    runs: int,
    latency_ms: int,
    rate_kib: int,
    fault_stall: int,
    fault_reset: int,
    fault_503: int,
    fault_tail_slow: int,
    fault_tail_slow_once: int,
    fault_slow_conn: int,
    part_size_mib: int,
    fast_timeouts: bool,
    urls_file: Optional[str],
    impersonate: Optional[str],
    adaptive: bool,
    procs: int,
    no_read1: bool,
    fault_profile: Optional[str],
    seed: int,
) -> None:
    """Benchmark downloader throughput vs worker count, with optional fault injection."""
    worker_counts = [int(w) for w in workers.split(",") if w.strip()]
    faults = {
        "stall": fault_stall,
        "reset": fault_reset,
        "http503": fault_503,
        "tail_slow": fault_tail_slow,
        "tail_slow_once": fault_tail_slow_once,
        "slow_conn": fault_slow_conn,
    }
    server: Optional[SegmentServer] = None
    session: Optional[Any] = None
    saved: dict[str, Any] = {}
    saved_network: Optional[dict] = None
    total = segments
    expected_bytes: Optional[bytes] = None

    if fast_timeouts:
        fast = {"READ_TIMEOUT": 2, "RETRY_WAIT": 1, "HEDGE_MIN_WAIT": 1.0}
        saved = {k: getattr(dl_mod, k) for k in fast}
        for k, v in fast.items():
            setattr(dl_mod, k, v)
        saved_network = config.network
        config.network = {**config.network, "read_timeout": fast["READ_TIMEOUT"]}
        os.environ["UNSHACKLE_DL_TIMING_OVERRIDES"] = ",".join(f"{k}={v}" for k, v in fast.items())

    if part_size_mib and hasattr(dl_mod, "RANGE_PARALLEL_PART_SIZE"):
        saved["RANGE_PARALLEL_PART_SIZE"] = dl_mod.RANGE_PARALLEL_PART_SIZE
        dl_mod.RANGE_PARALLEL_PART_SIZE = part_size_mib * MIB

    if no_read1:
        if hasattr(dl_mod, "RACER_READ1"):
            saved["RACER_READ1"] = dl_mod.RACER_READ1
            dl_mod.RACER_READ1 = False
        else:
            console.print("[yellow]--no-read1 ignored: only applies to older checkouts that still have RACER_READ1[/]")

    if impersonate:
        from unshackle.core.session import session as make_session

        session = make_session(impersonate)

    try:
        if fault_profile:
            if urls_file:
                console.print("[yellow]--urls-file ignored in --fault-profile mode[/]")
            if any(faults.values()):
                console.print("[yellow]--fault-* flags ignored in --fault-profile mode (use the profile knobs)[/]")
            _procs_notice(procs, segments, session)
            run_fault_mode(fault_profile, segments, seg_size, worker_counts, runs, session, adaptive, procs, seed)
            return
        if urls_file:
            if any(faults.values()):
                console.print("[yellow]fault flags ignored in --urls-file mode[/]")
            urls = [ln.strip() for ln in Path(urls_file).read_text().splitlines() if ln.strip()]
            if not urls:
                raise click.ClickException("--urls-file contained no URLs")
            total = len(urls)
            _procs_notice(procs, len(urls), session)
            console.print(f"[cyan]URL-file mode:[/] {len(urls)} URLs, impersonate={impersonate or 'none'}")
        else:
            buffer = (b"unshackle-bench-" * (seg_size // 16 + 1))[:seg_size]
            expected_bytes = buffer
            server = SegmentServer(buffer, latency_ms, rate_kib, faults, segments)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            host, port = str(server.server_address[0]), int(server.server_address[1])
            urls = [f"http://{host}:{port}/seg/{i}" for i in range(segments)]
            active = {k: v for k, v in faults.items() if v}
            _procs_notice(procs, segments, session)
            console.print(
                f"[cyan]Synthetic mode:[/] {segments} x {seg_size / MIB:.2f} MiB "
                f"(latency={latency_ms}ms, rate={rate_kib or 'unbounded'} KiB/s, "
                f"faults={active or 'none'}, fast_timeouts={fast_timeouts}, impersonate={impersonate or 'none'})"
            )

        rows = sweep(urls, worker_counts, runs, session, server, expected_bytes, adaptive, procs)
        print_report(rows, runs, total)

        failed = {row[0]: row[6] for row in rows if row[6]}
        if failed:
            detail = "; ".join(f"workers={w}: segments {miss}" for w, miss in failed.items())
            raise click.ClickException(f"incomplete/corrupt downloads: {detail}")
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        raise SystemExit(130) from None
    finally:
        if server is not None:
            server.stall_event.set()
            server.shutdown()
        for k, v in saved.items():
            setattr(dl_mod, k, v)
        if saved_network is not None:
            config.network = saved_network
        os.environ.pop("UNSHACKLE_DL_TIMING_OVERRIDES", None)


if __name__ == "__main__":
    main()
