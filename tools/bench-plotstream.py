"""Streaming-sampler bench: the `var sample` kHz path on silicon.

Measures the achieved rate of the backend streamer (test-plan §11 16h v2):
one signal at the full budget, the 2- and 4-signal splits, and a stepline
issued mid-stream (commands must interleave between batches and the stream
must continue). Records under bench-runs/<date>-plotstream/.

Usage:  python tools/bench-plotstream.py [path\\to\\app.elf]
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
OUTDIR = REPO / "bench-runs" / (time.strftime("%Y%m%d") + "-plotstream")

U16 = "u16@0x317BA1D0:16"      # cdd_ain TEST_cnt, wraps at 1000 once/s
U32 = "u32@0x317BABD0:32"      # cdd_btn9970 static TEST_cnt, ramp
PAD1 = "pad1@0x317BA1D4:32"    # arbitrary RAM neighbours for the 4-way split
PAD2 = "pad2@0x317BA1D8:32"


class Session:
    """Machine-mode child with timestamped capture (bench-driver shape)."""

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

    def send(self, cmd: str) -> int:
        self.seq += 1
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        return self.seq

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

    def run(self, cmd: str, timeout: float = 60.0) -> dict:
        seq = self.send(cmd)
        _, env = self.wait_env(seq, timeout)
        return env

    def wait_ready(self) -> dict:
        deadline = time.monotonic() + 120
        while True:
            item = self._next_line(deadline)
            if item is None:
                raise TimeoutError("backend never became ready")
            if item[1].get("event") == "ready":
                return item[1]

    def collect(self, seconds: float) -> list[tuple[float, dict]]:
        """Drain lines for a wall-clock window; returns samples events."""
        out: list[tuple[float, dict]] = []
        deadline = time.monotonic() + seconds
        while True:
            item = self._next_line(deadline)
            if item is None:
                return out
            if item[1].get("event") == "samples":
                out.append(item)


def rates(events: list[tuple[float, dict]]) -> dict[str, dict]:
    """Per-signal achieved rate + timestamp regularity from samples events."""
    per: dict[str, list[float]] = {}
    values: dict[str, list[int]] = {}
    for _, ev in events:
        for sig in ev["signals"]:
            per.setdefault(sig["name"], []).extend(
                ev["t0_ms"] + dt for dt in sig["dt"]
            )
            values.setdefault(sig["name"], []).extend(sig["v"])
    out: dict[str, dict] = {}
    for name, stamps in per.items():
        if len(stamps) < 2:
            out[name] = {"samples": len(stamps)}
            continue
        duration = (stamps[-1] - stamps[0]) / 1000.0
        deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b >= a]
        vals = values[name]
        wraps = sum(
            1 for a, b in zip(vals, vals[1:]) if b < a - (max(vals) - min(vals)) / 2
        ) if len(set(vals)) > 1 else 0
        out[name] = {
            "samples": len(stamps),
            "achieved_hz": round((len(stamps) - 1) / duration, 1) if duration else 0,
            "median_dt_ms": round(statistics.median(deltas), 3) if deltas else None,
            "distinct_values": len(set(vals)),
            "wraps": wraps,
        }
    return out


def main() -> int:
    if not ELF.is_file():
        print(f"ELF not found: {ELF}")
        return 2
    s = Session()
    print(f"stream bench: {ELF} on {CORE} -> {OUTDIR}", flush=True)
    summary = [
        "# Streaming sampler bench (`var sample`, test-plan §11 16h v2)",
        "",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}   elf: {ELF}   core: {CORE}",
        "",
    ]
    try:
        ready = s.wait_ready()
        if ready.get("discovery_error"):
            s.run("gates apply", 60)
            s.run("core list", 60)
        if not s.run(f'sym load "{ELF}"', 300).get("ok"):
            raise RuntimeError("sym load failed")
        if not s.run(f'load "{ELF}"', 600).get("ok"):
            raise RuntimeError("load failed")
        s.run(f"core resume --core {CORE}", 60)
        time.sleep(3.0)
        print("  app booted", flush=True)

        def phase(label: str, signals: list[str], seconds: float) -> None:
            specs = " ".join(f'--signal "{sig}"' for sig in signals)
            env = s.run(f"var sample start {specs} --rate 1000", 60)
            assert env.get("ok"), env
            events = s.collect(seconds)
            stats = rates(events)
            print(f"  {label}: {json.dumps(stats)}", flush=True)
            summary.append(f"## {label} — rate 1000 requested, {seconds:.0f} s")
            for name, st in stats.items():
                summary.append(f"- **{name}**: {json.dumps(st)}")
            summary.append("")

        phase("S1 one signal", [U16], 10)
        phase("S2 two signals", [U16, U32], 10)
        phase("S3 four signals", [U16, U32, PAD1, PAD2], 6)

        # S4: a source-line step issued MID-STREAM -- it must interleave
        # between batches and the stream must carry on.
        s.run(f'var sample start --signal "{U16}" --rate 1000', 60)
        s.collect(2)
        s.run(f"core halt --core {CORE}", 60)
        t0 = time.perf_counter()
        step_env = s.run(f"stepline --core {CORE}", 90)
        step_ms = round((time.perf_counter() - t0) * 1000, 1)
        s.run(f"core resume --core {CORE}", 60)
        after = s.collect(3)
        stats = rates(after)
        print(f"  S4 stepline mid-stream: step={step_ms}ms "
              f"stream-after={json.dumps(stats)}", flush=True)
        summary.append("## S4 stepline mid-stream")
        summary.append(
            f"- stepline while streaming: {step_ms} ms "
            f"(stopped: {(step_env.get('result') or {}).get('stopped')}); "
            f"stream after resume: {json.dumps(stats)}"
        )
        summary.append("")
        s.run("var sample stop", 60)
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
        out = OUTDIR / "02-summary.md"
        out.write_text("\n".join(summary) + "\n", encoding="utf-8")
        print(f"summary -> {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
