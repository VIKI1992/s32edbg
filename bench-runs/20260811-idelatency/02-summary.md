# Interactive-latency bench (test-plan §11 step 15)

date: 2026-08-11 13:48:04   elf: C:\br1_rear_ecu\build\bin\br1_rear.elf
core: rtu0.0   speed: 16000 kHz   driver: tools/bench-idelatency.py

| operation | backend ms | DP/AP ops | extra |
|---|---:|---:|---|
| sym load | 36159.0 | 0 |  |
| load-to-RAM | 35508.6 | 27926 |  |
| boot resume | 157.3 | 162 |  |
| core sample | 1213.5 | 1254 |  |
| bp set (hot) | 50.2 | 48 |  |
| first hit |  |  | status=halted: breakpoint |
| continue 1 | 366.2 | 378 | detect_ms=366.4  rehalted=True  monitor_push_ms=389.7 |
| continue 2 | 363.6 | 378 | detect_ms=363.8  rehalted=True  monitor_push_ms=386.7 |
| continue 3 | 365.6 | 378 | detect_ms=365.8  rehalted=True  monitor_push_ms=388.5 |
| continue 4 | 365.0 | 378 | detect_ms=365.3  rehalted=True  monitor_push_ms=388.1 |
| regs cold | 301.0 | 306 |  |
| regs warm | 18.2 | 12 |  |
| stack 32 | 76.6 | 73 |  |
| var locals cold | 1129.8 | 13 |  |
| var locals warm | 15.8 | 12 |  |
| var globals p5 | 72.4 | 73 |  |
| var watch __func__ | 13.6 | 13 |  |
| disasm | 184.4 | 96 |  |
| stepline ranged 1 | 485.2 | 504 | steps=1  stopped=line-change |
| stepline ranged 2 | 318.6 | 330 | steps=1  stopped=line-change |
| stepline ranged 3 | 513.1 | 534 | steps=2  stopped=line-change |
| stepline ranged 4 | 276.7 | 288 | steps=1  stopped=line-change |
| stepline ranged 5 | 319.8 | 330 | steps=1  stopped=line-change |
| stepline ranged 6 | 514.0 | 534 | steps=2  stopped=line-change |
| stepline legacy 1 | 248.8 | 258 | steps=1  stopped=line-change |
| stepline legacy 2 | 251.1 | 258 | steps=1  stopped=line-change |
| stepline legacy 3 | 640.9 | 666 | steps=3  stopped=line-change |
| stepline legacy 4 | 249.1 | 258 | steps=1  stopped=line-change |
| stepline legacy 5 | 640.5 | 666 | steps=3  stopped=line-change |
| stepline legacy 6 | 248.7 | 258 | steps=1  stopped=line-change |
| step into 1 | 250.1 | 258 |  |
| step into 2 | 248.4 | 258 |  |
| step into 3 | 249.1 | 258 |  |
| step over | 394.9 | 410 |  |
| poll | 157.9 | 162 |  |

continue push-detection latency (resume send -> stopped event): median 366 ms over 4 runs (366, 364, 366, 365)
stepline median: ranged 402 ms vs legacy 250 ms

## Reading the numbers

- Third session of the day; the first two runs surfaced and fixed (a) missed
  stopped events when a breakpoint re-hits inside the resume (`rehalted` now
  in the envelope + the monitor is armed anyway) and (b) two per-hop
  overheads (phantom step-off, hardware free-slot search) worth ~250 ms.
- Every continue here re-hit the ADC breakpoint before the restart poll --
  the stop arrives IN the resume envelope (`rehalted`), so a frontend's
  perceived latency is the resume round trip itself (~365 ms, of which
  ~250 ms is the architecturally required step-off past the armed
  comparator). The monitor's confirming push lands ~23 ms later.
- The stepped RTD driver lines are 1-3 instructions (`steps` column):
  ranged does 1-2 units where legacy does 1-3 steps, so the two are close
  here. The range-stepper's advantage scales with line length -- the
  original complaint was 20+-instruction -Og application lines at
  ~0.4 s/instruction (3-12 s/line); any such line is now one ~0.3 s hop.
- Baseline for the same operations before this work (cost model, 2026-08-11
  diagnosis): step ~0.5-0.6 s, stop refresh 3-6 s serialized (regs 0.65 s
  x3-5, stack 0.8-1.0 s, watch 0.9 s each), continue detection up to 1-3 s
  polled. Measured now: full stop refresh under 0.5 s cold, step 0.25 s,
  detection pushed.
- Host-side, once per session, 0 probe ops: `sym load` 36.2 s (pyelftools
  DWARF parse of the 29 MB production ELF) and the first `var locals`
  +1.1 s (VariableIndex build). These now dominate connect/first-stop and
  are the next optimisation target (persist the parsed index keyed on ELF
  identity) -- recorded as Finding 17.
