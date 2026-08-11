"""End-to-end sampling test for Plot Variable (test-plan §11 16h, backend side).

Drives one machine-mode session exactly the way a plot panel's sampler does:
frameless `var watch` resolve, `var refresh <handle>` at a target rate, and
stale-handle re-resolve after a run-state change. Samples the user's test
counters (TEST_cnt: a wrap-at-1000 uint16 saw-tooth in cdd_ain.c and an
unbounded uint32 ramp static in cdd_btn9970.c), then writes CSVs (the panel's
export format), a standalone SVG rendering of the captured waveforms, and a
summary with computed verdicts: achieved rate, wrap count and saw period,
recovery behaviour.

Usage:  python tools/bench-plotsample.py [path\\to\\app.elf]
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ELF = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    r"C:\br1_rear_ecu\build\bin\br1_rear.elf"
)
CORE = "rtu0.0"
SPEED = "16000"
OUTDIR = REPO / "bench-runs" / (time.strftime("%Y%m%d") + "-plotsample")


class Session:
    """Machine-mode child with timestamped line capture (same shape as
    tools/bench-idelatency.py, kept standalone like the other tools)."""

    def __init__(self) -> None:
        OUTDIR.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "s32edbg", "--speed", SPEED,
             "debug", "--machine", "--core", CORE],
            cwd=str(REPO),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        self.lines: list[tuple[float, dict]] = []
        self.cursor = 0
        self.seq = 0
        self.cv = threading.Condition()
        self.transcript = open(OUTDIR / "01-transcript.ndjson", "w", encoding="utf-8")
        self.sends = open(OUTDIR / "00-sends.log", "w", encoding="utf-8")
        self.stderr_log = open(OUTDIR / "03-stderr.log", "w", encoding="utf-8")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            t = time.perf_counter()
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = {"unparseable": raw}
            self.transcript.write(json.dumps({"t": round(t, 6), **obj}) + "\n")
            with self.cv:
                self.lines.append((t, obj))
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_log.write(line)

    def send(self, cmd: str) -> tuple[int, float]:
        self.seq += 1
        t = time.perf_counter()
        self.sends.write(f"{t:.6f} >> {cmd}\n")
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        return self.seq, t

    def _next_line(self, deadline: float) -> tuple[float, dict] | None:
        with self.cv:
            while self.cursor >= len(self.lines):
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return None
                self.cv.wait(min(left, 0.5))
            item = self.lines[self.cursor]
            self.cursor += 1
            return item

    def wait_env(self, seq: int, timeout: float) -> tuple[float, dict]:
        deadline = time.monotonic() + timeout
        while True:
            item = self._next_line(deadline)
            if item is None:
                raise TimeoutError(f"no envelope for seq {seq}")
            t, obj = item
            if "event" in obj or "unparseable" in obj:
                continue
            if obj.get("seq") == seq:
                return t, obj

    def run(self, cmd: str, timeout: float = 60.0) -> tuple[float, float, dict]:
        seq, t_send = self.send(cmd)
        t_env, env = self.wait_env(seq, timeout)
        return t_send, t_env, env

    def wait_ready(self) -> dict:
        deadline = time.monotonic() + 120
        while True:
            item = self._next_line(deadline)
            if item is None:
                raise TimeoutError("backend never became ready")
            if item[1].get("event") == "ready":
                return item[1]


class Target:
    def __init__(self, name: str, path: str, via: str, row: dict) -> None:
        self.name = name
        self.path = path
        self.via = via  # "watch" | "globals"
        self.handle: str | None = row.get("handle")
        self.address: str | None = row.get("address")
        self.kind = row.get("kind")
        self.samples: list[tuple[float, int]] = []  # (wall s, value)
        self.nulls = 0
        self.recoveries = 0


def resolve(s: Session, t: Target) -> bool:
    """Re-mint a handle the way the panel does: by path for the plain name,
    by address match over a filtered globals page for the qualified static."""
    if t.via == "watch":
        _, _, env = s.run(f'var watch "{t.path}" --core {CORE}', 60)
        row = (env.get("result") or {}).get("row") or {}
        if env.get("ok") and row.get("handle") and row.get("address"):
            t.handle = row["handle"]
            t.address = row["address"]
            t.kind = row.get("kind")
            return True
        return False
    _, _, env = s.run(f'var globals --core {CORE} --filter TEST_cnt --limit 10', 60)
    for row in ((env.get("result") or {}).get("variables") or []):
        if row.get("address") == t.address and row.get("handle"):
            t.handle = row["handle"]
            return True
    return False


def sample_phase(
    s: Session, targets: list[Target], hz: float, seconds: float, label: str,
) -> dict:
    period = 1.0 / hz
    t_end = time.monotonic() + seconds
    sent = 0
    next_at = time.monotonic()
    while time.monotonic() < t_end:
        for t in targets:
            if t.handle is None and not resolve(s, t):
                continue
            _, t_env, env = s.run(f"var refresh {t.handle}", 30)
            sent += 1
            if not env.get("ok"):
                message = ((env.get("error") or {}).get("message")) or ""
                if message.startswith("stale-handle:"):
                    t.handle = None
                    if resolve(s, t):
                        t.recoveries += 1
                    continue
                t.nulls += 1
                continue
            rows = (env.get("result") or {}).get("rows") or []
            value = rows[0].get("value") if rows else None
            if not value or not value.get("hex"):
                t.nulls += 1
                continue
            t.samples.append((t_env, int(value["hex"], 16)))
        next_at += period
        pause = next_at - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        else:
            next_at = time.monotonic()
    return {"label": label, "hz": hz, "seconds": seconds, "sent": sent}


def analyse(t: Target, t0: float, t1: float) -> dict:
    window = [(ts, v) for ts, v in t.samples if t0 <= ts <= t1]
    if len(window) < 2:
        return {"samples": len(window)}
    values = [v for _, v in window]
    duration = window[-1][0] - window[0][0]
    increases = sum(
        1 for a, b in zip(values, values[1:]) if b > a
    )
    wraps = [
        window[i + 1][0]
        for i in range(len(values) - 1)
        if values[i + 1] < values[i] - (max(values) - min(values)) / 2
    ]
    periods = [b - a for a, b in zip(wraps, wraps[1:])]
    return {
        "samples": len(window),
        "achieved_hz": round((len(window) - 1) / duration, 2) if duration else 0,
        "min": min(values),
        "max": max(values),
        "rising_fraction": round(increases / (len(values) - 1), 3),
        "wraps": len(wraps),
        "saw_period_s": round(statistics.median(periods), 3) if periods else None,
    }


def write_csv(t: Target) -> Path:
    name = t.name.replace(":", "_")
    out = OUTDIR / f"samples-{name}.csv"
    epoch = time.time() - time.perf_counter()
    with open(out, "w", encoding="utf-8") as f:
        f.write("timestamp_ms,value\n")
        for ts, v in t.samples:
            f.write(f"{int((epoch + ts) * 1000)},{v}\n")
    return out


def write_svg(targets: list[Target]) -> Path:
    """A standalone light-surface rendering: one panel per series, 2px line
    in the validated light-mode series color, recessive grid, text tokens."""
    width, panel_h, pad = 960, 220, 46
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{len(targets) * panel_h}" font-family="Segoe UI, sans-serif" '
        'font-size="12">',
        f'<rect width="{width}" height="{len(targets) * panel_h}" fill="#fcfcfb"/>',
    ]
    for i, t in enumerate(targets):
        y0 = i * panel_h
        plot_w, plot_h = width - pad - 12, panel_h - 52
        parts.append(f'<g transform="translate({pad},{y0 + 30})">')
        if len(t.samples) >= 2:
            ts0 = t.samples[0][0]
            span_t = max(t.samples[-1][0] - ts0, 1e-6)
            vmin = min(v for _, v in t.samples)
            vmax = max(v for _, v in t.samples)
            if vmin == vmax:
                vmax = vmin + 1
            pts = " ".join(
                f"{(ts - ts0) / span_t * plot_w:.1f},"
                f"{(1 - (v - vmin) / (vmax - vmin)) * plot_h:.1f}"
                for ts, v in t.samples
            )
            for frac in (0, 0.5, 1):
                gy = frac * plot_h
                parts.append(
                    f'<line x1="0" y1="{gy:.0f}" x2="{plot_w}" y2="{gy:.0f}" '
                    'stroke="#d8d8d4" stroke-width="1"/>'
                )
            parts.append(
                f'<text x="-8" y="4" text-anchor="end" fill="#666">{vmax}</text>'
            )
            parts.append(
                f'<text x="-8" y="{plot_h + 4:.0f}" text-anchor="end" '
                f'fill="#666">{vmin}</text>'
            )
            parts.append(
                f'<polyline points="{pts}" fill="none" stroke="#1a6fd4" '
                'stroke-width="2" stroke-linejoin="round"/>'
            )
            parts.append(
                f'<text x="0" y="-10" fill="#222" font-weight="600">{t.name}'
                f'  ({len(t.samples)} samples, {t.samples[-1][0] - ts0:.1f} s)</text>'
            )
        else:
            parts.append(f'<text x="0" y="0" fill="#666">{t.name}: no samples</text>')
        parts.append("</g>")
    parts.append("</svg>")
    out = OUTDIR / "waveform.svg"
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def main() -> int:
    if not ELF.is_file():
        print(f"ELF not found: {ELF}")
        return 2
    s = Session()
    print(f"plot-sample test: {ELF} on {CORE} -> {OUTDIR}", flush=True)
    summary: list[str] = [
        "# Plot Variable sampling test (test-plan §11 16h, backend contract)",
        "",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}   elf: {ELF}   core: {CORE}",
        "",
    ]
    targets: list[Target] = []
    phases: list[tuple[dict, float, float]] = []
    try:
        ready = s.wait_ready()
        if ready.get("discovery_error"):
            s.run("gates apply", 60)
            s.run("core list", 60)
        _, _, env = s.run(f'sym load "{ELF}"', 300)
        if not env.get("ok"):
            raise RuntimeError("sym load failed")
        print("  symbols loaded", flush=True)
        _, _, env = s.run(f'load "{ELF}"', 600)
        if not env.get("ok"):
            raise RuntimeError("load failed")
        s.run(f"core resume --core {CORE}", 60)
        time.sleep(3.0)
        print("  app booted from RAM", flush=True)

        # Resolve exactly like the panel: plain path first...
        _, _, env = s.run(f'var watch "TEST_cnt" --core {CORE}', 60)
        row = (env.get("result") or {}).get("row") or {}
        if env.get("ok") and row.get("handle") and row.get("address"):
            targets.append(Target("watch-TEST_cnt", "TEST_cnt", "watch", row))
            print(f"  watch TEST_cnt -> {row.get('address')} kind={row.get('kind')}",
                  flush=True)
        # ...then any OTHER TEST_cnt the globals page knows (the function
        # static), matched by address.
        _, _, env = s.run(f'var globals --core {CORE} --filter TEST_cnt --limit 10', 60)
        for grow in ((env.get("result") or {}).get("variables") or []):
            if "TEST_cnt" not in (grow.get("path") or ""):
                continue
            if targets and grow.get("address") == targets[0].address:
                continue
            if grow.get("handle") and grow.get("address"):
                targets.append(
                    Target(f"globals-{grow['path']}", grow["path"], "globals", grow)
                )
                print(f"  globals {grow['path']} -> {grow['address']} "
                      f"kind={grow.get('kind')}", flush=True)
        if not targets:
            raise RuntimeError("no sampleable TEST_cnt found in this ELF")

        for label, hz, seconds in (("A 10 Hz", 10.0, 20.0), ("B 25 Hz", 25.0, 10.0)):
            t0 = time.perf_counter()
            info = sample_phase(s, targets, hz, seconds, label)
            phases.append((info, t0, time.perf_counter()))
            print(f"  phase {label}: sent {info['sent']}", flush=True)

        # Phase C: a run-state change mid-sampling -- the stale-handle
        # recovery the panel depends on.
        s.run(f"core halt --core {CORE}", 60)
        s.run(f"stepline --core {CORE}", 90)
        s.run(f"core resume --core {CORE}", 60)
        t0 = time.perf_counter()
        info = sample_phase(s, targets, 10.0, 5.0, "C recovery")
        phases.append((info, t0, time.perf_counter()))
        recoveries = sum(t.recoveries for t in targets)
        print(f"  phase C: sent {info['sent']}, recoveries {recoveries}", flush=True)
        return 0
    finally:
        try:
            if s.proc.poll() is None:
                s.send("quit")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    item = s._next_line(deadline)
                    if item is None or item[1].get("event") == "bye":
                        if item:
                            print(f"  bye: {item[1].get('restore_notes')}", flush=True)
                        break
                s.proc.wait(timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(f"  shutdown: {exc}", flush=True)
            s.proc.kill()
        for info, t0, t1 in phases:
            summary.append(f"## Phase {info['label']} — target {info['hz']} Hz "
                           f"for {info['seconds']} s (sent {info['sent']})")
            for t in targets:
                summary.append(f"- **{t.name}** ({t.kind} @ {t.address}): "
                               f"{json.dumps(analyse(t, t0, t1))}")
            summary.append("")
        for t in targets:
            summary.append(
                f"- {t.name}: {len(t.samples)} samples total, {t.nulls} null "
                f"ticks, {t.recoveries} stale-handle recoveries "
                f"-> {write_csv(t).name}"
            )
        if targets:
            summary.append(f"- waveform: {write_svg(targets).name}")
        out = OUTDIR / "02-summary.md"
        out.write_text("\n".join(summary) + "\n", encoding="utf-8")
        print(f"summary -> {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
