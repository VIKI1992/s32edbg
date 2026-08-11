"""Interactive-latency bench: test-plan §11 step 15, driven headless.

One machine-mode session (one connect -- the D-2 rule), scripted:

  load ELF to RAM -> boot -> sample a hot address -> breakpoint it ->
  measure continue-to-stop push latency x4 -> at the stop measure
  regs (cold/warm), stack, locals, watch, disasm -> stepline x6 ranged vs
  x6 --no-range-step -> instruction steps -> poll -> quit (restore ledger).

Every stdout line is timestamped on arrival, so the `stopped` push events
are measured wall-clock from the `resume` SEND, exactly what a user feels.
Per-command `ms`/`ops` come from the envelopes themselves.

Records under bench-runs/<date>-idelatency/:
  00-sends.log          timestamped lines sent
  01-transcript.ndjson  every stdout line, with arrival timestamp
  02-summary.md         the table the test plan asks for
  03-stderr.log         the human/Rich stream

Usage:  python tools/bench-idelatency.py [path\\to\\app.elf]
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
OUTDIR = REPO / "bench-runs" / (time.strftime("%Y%m%d") + "-idelatency")


class Session:
    """The machine-mode child, with in-order consumption and side-tracking
    of stopped events (they may legally arrive before the envelope of the
    command that caused them)."""

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
        self.pending_stops: list[tuple[float, dict]] = []
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
            self.transcript.flush()
            with self.cv:
                self.lines.append((t, obj))
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:
            self.stderr_log.write(line)
            self.stderr_log.flush()

    def send(self, cmd: str) -> tuple[int, float]:
        self.seq += 1
        t = time.perf_counter()
        self.sends.write(f"{t:.6f} >> {cmd}\n")
        self.sends.flush()
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
        """Consume lines until seq's envelope; stopped events seen on the
        way are queued for wait_stopped, progress lines are dropped."""
        deadline = time.monotonic() + timeout
        while True:
            item = self._next_line(deadline)
            if item is None:
                raise TimeoutError(f"no envelope for seq {seq} within {timeout:.0f}s")
            t, obj = item
            if obj.get("event") == "stopped":
                self.pending_stops.append((t, obj))
                continue
            if "event" in obj or "unparseable" in obj:
                continue
            if obj.get("seq") == seq:
                return t, obj

    def wait_stopped(self, timeout: float) -> tuple[float, dict] | None:
        if self.pending_stops:
            return self.pending_stops.pop(0)
        deadline = time.monotonic() + timeout
        while True:
            item = self._next_line(deadline)
            if item is None:
                return None
            t, obj = item
            if obj.get("event") == "stopped":
                return t, obj

    def run(self, cmd: str, timeout: float = 60.0) -> tuple[float, float, dict]:
        """Send + wait; returns (t_send, t_envelope, envelope)."""
        seq, t_send = self.send(cmd)
        t_env, env = self.wait_env(seq, timeout)
        return t_send, t_env, env


ROWS: list[dict] = []


def note(label: str, env: dict | None = None, **extra) -> None:
    row = {"label": label, **extra}
    if env is not None:
        row["ms"] = env.get("ms")
        row["ops"] = env.get("ops")
        row["ok"] = env.get("ok")
        if not env.get("ok"):
            row["error"] = (env.get("error") or {}).get("message")
    ROWS.append(row)
    bits = "  ".join(f"{k}={v}" for k, v in row.items() if k != "label")
    print(f"  {label}: {bits}", flush=True)


def main() -> int:
    if not ELF.is_file():
        print(f"ELF not found: {ELF}")
        return 2
    s = Session()
    print(f"bench: {ELF} on {CORE} at {SPEED} kHz -> {OUTDIR}", flush=True)
    detections: list[float] = []
    try:
        # ready is an event, not an envelope: wait for it directly.
        ready: dict | None = None
        deadline = time.monotonic() + 120
        while True:
            item = s._next_line(deadline)
            if item is None:
                raise TimeoutError("backend never became ready")
            t, obj = item
            if obj.get("event") == "ready":
                ready = obj
                break
        print(f"  ready: cores={ready.get('cores')} "
              f"discovery_error={ready.get('discovery_error')}", flush=True)
        if ready.get("discovery_error"):
            _, _, env = s.run("gates apply", 60)
            note("gates apply", env)
            _, _, env = s.run("core list", 60)
            note("core list", env)

        _, _, env = s.run(f'sym load "{ELF}"', 300)
        note("sym load", env)
        if not env.get("ok"):
            raise RuntimeError("sym load failed; cannot bench source stepping")

        # ---- load to RAM (resets the SoC, parks rtu0.0 at entry) ----------
        _, _, env = s.run(f'load "{ELF}"', 600)
        note("load-to-RAM", env)
        if not env.get("ok"):
            raise RuntimeError("load failed; not benching against unknown flash")

        _, _, env = s.run(f"core resume --core {CORE}", 60)
        note("boot resume", env)
        time.sleep(3.0)  # let the application boot

        # ---- find a hot, reachable address --------------------------------
        _, _, env = s.run(f"core sample --core {CORE} --count 200", 120)
        note("core sample", env, )
        top = (env.get("result") or {}).get("top") or []
        if not top:
            raise RuntimeError("no PC samples; is the core running?")

        # Prefer a hot address OUTSIDE the hottest function: on this app the
        # top sample is the two-instruction idle loop, which has no
        # straight-line stretch for the range-stepper to show and re-hits a
        # breakpoint before the restart poll can see the core running.
        def func_of(addr: str) -> tuple[str | None, dict]:
            _, _, e = s.run(f"sym addr2line {addr}", 60)
            hit = ((e.get("result") or {}).get("lookups") or [{}])[0]
            return hit.get("func"), hit

        idle_func, _ = func_of(top[0]["address"])
        hot, hot_hit = top[0]["address"], {}
        for row in top[1:6]:
            fn, hit = func_of(row["address"])
            if fn and fn != idle_func and hit.get("file") and hit.get("line"):
                hot, hot_hit = row["address"], hit
                break
        print(
            f"  site: {hot} func={hot_hit.get('func', idle_func)} "
            f"{hot_hit.get('file')}:{hot_hit.get('line')} "
            f"(idle func: {idle_func})",
            flush=True,
        )

        _, _, env = s.run(f"bp set {hot} --core {CORE}", 60)
        note("bp set (hot)", env)

        # The boot resume armed the halt monitor; the hit is pushed.
        got = s.wait_stopped(15)
        if got is None:
            note("first hit", detect_ms=None, missed=True)
            _, _, env = s.run(f"core halt --core {CORE}", 60)
            note("manual halt", env)
        else:
            t_stop, ev = got
            note("first hit", status=ev.get("status"))

        # ---- continue x4: push-detection latency --------------------------
        # Two legitimate outcomes per continue: the resume envelope itself
        # reports `rehalted` (the breakpoint re-hit before the restart poll
        # -- a frontend reacts instantly from the envelope), or the core
        # runs and the halt monitor pushes a stopped event. Both are timed
        # from the resume SEND: that is what a user feels.
        for i in range(4):
            t_send, t_env, env = s.run(f"core resume --core {CORE}", 60)
            row = ((env.get("result") or {}).get("resume") or [{}])[0]
            if row.get("rehalted"):
                detect = (t_env - t_send) * 1000.0
                detections.append(detect)
                got = s.wait_stopped(3)  # the monitor's confirming push
                push = round((got[0] - t_send) * 1000.0, 1) if got else None
                note(f"continue {i + 1}", env, detect_ms=round(detect, 1),
                     rehalted=True, monitor_push_ms=push)
                continue
            got = s.wait_stopped(15)
            if got is None:
                note(f"continue {i + 1}", env, detect_ms=None, missed=True)
                _, _, henv = s.run(f"core halt --core {CORE}", 60)
                note("manual halt", henv)
                continue
            t_stop, ev = got
            detect = (t_stop - t_send) * 1000.0
            detections.append(detect)
            note(f"continue {i + 1}", env, detect_ms=round(detect, 1),
                 at=(ev.get("location") or {}).get("address"))

        # ---- the per-stop refresh set ------------------------------------
        _, _, env = s.run("regs", 60)
        note("regs cold", env)
        _, _, env = s.run("regs", 60)
        note("regs warm", env)
        _, _, env = s.run("stack --max-frames 32", 60)
        note("stack 32", env)
        _, _, env = s.run("var locals --frame 0", 120)
        note("var locals cold", env)
        _, _, env = s.run("var locals --frame 0", 120)
        note("var locals warm", env)
        _, _, env = s.run("var globals --limit 5", 120)
        note("var globals p5", env)
        watch_path = None
        for row in ((env.get("result") or {}).get("variables") or []):
            if row.get("address") and not row.get("error"):
                watch_path = row["path"]
                break
        if watch_path:
            _, _, env = s.run(f'var watch "{watch_path}"', 60)
            note(f"var watch {watch_path}", env)
        _, _, env = s.run("disasm", 60)
        note("disasm", env)

        # ---- stepline: ranged vs legacy ----------------------------------
        for i in range(6):
            _, _, env = s.run(f"stepline --core {CORE}", 90)
            r = env.get("result") or {}
            note(f"stepline ranged {i + 1}", env, steps=r.get("steps"),
                 stopped=r.get("stopped"))
        for i in range(6):
            _, _, env = s.run(f"stepline --core {CORE} --no-range-step", 120)
            r = env.get("result") or {}
            note(f"stepline legacy {i + 1}", env, steps=r.get("steps"),
                 stopped=r.get("stopped"))

        # ---- instruction steps -------------------------------------------
        for i in range(3):
            _, _, env = s.run(f"core step --core {CORE} --into", 60)
            note(f"step into {i + 1}", env)
        _, _, env = s.run(f"core step --core {CORE} --over", 60)
        note("step over", env)

        _, _, env = s.run("poll", 60)
        note("poll", env)
        return 0
    finally:
        try:
            if s.proc.poll() is None:
                s.send("quit")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    item = s._next_line(deadline)
                    if item is None:
                        break
                    if item[1].get("event") == "bye":
                        print(f"  bye: {item[1].get('restore_notes')}", flush=True)
                        break
                s.proc.wait(timeout=20)
        except Exception as exc:  # noqa: BLE001
            print(f"  shutdown: {exc}", flush=True)
            s.proc.kill()
        _write_summary(detections)


def _write_summary(detections: list[float]) -> None:
    lines = [
        "# Interactive-latency bench (test-plan §11 step 15)",
        "",
        f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}   elf: {ELF}",
        f"core: {CORE}   speed: {SPEED} kHz   driver: tools/bench-idelatency.py",
        "",
        "| operation | backend ms | DP/AP ops | extra |",
        "|---|---:|---:|---|",
    ]
    for row in ROWS:
        extra = "  ".join(
            f"{k}={v}" for k, v in row.items()
            if k not in ("label", "ms", "ops", "ok")
        )
        lines.append(
            f"| {row['label']} | {row.get('ms', '')} | {row.get('ops', '')} | {extra} |"
        )
    if detections:
        lines += [
            "",
            f"continue push-detection latency (resume send -> stopped event): "
            f"median {statistics.median(detections):.0f} ms over {len(detections)} "
            f"runs ({', '.join(f'{d:.0f}' for d in detections)})",
        ]
    ranged = [r for r in ROWS if r["label"].startswith("stepline ranged") and r.get("ms")]
    legacy = [r for r in ROWS if r["label"].startswith("stepline legacy") and r.get("ms")]
    if ranged and legacy:
        lines += [
            f"stepline median: ranged {statistics.median(r['ms'] for r in ranged):.0f} ms "
            f"vs legacy {statistics.median(r['ms'] for r in legacy):.0f} ms",
        ]
    out = OUTDIR / "02-summary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary -> {out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
