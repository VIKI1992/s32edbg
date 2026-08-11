# Streaming sampler bench (`var sample`, test-plan §11 16h v2)

date: 2026-08-11 17:22:40   elf: C:\br1_rear_ecu\build\bin\br1_rear.elf   core: rtu0.0

## S1 one signal — rate 1000 requested, 10 s
- **u16**: {"samples": 7450, "achieved_hz": 754.9, "median_dt_ms": 0.963, "distinct_values": 1000, "wraps": 10}

## S2 two signals — rate 1000 requested, 10 s
- **u16**: {"samples": 3625, "achieved_hz": 365.8, "median_dt_ms": 0.967, "distinct_values": 996, "wraps": 10}
- **u32**: {"samples": 3625, "achieved_hz": 365.8, "median_dt_ms": 0.968, "distinct_values": 130, "wraps": 0}

## S3 four signals — rate 1000 requested, 6 s
- **u16**: {"samples": 1020, "achieved_hz": 172.4, "median_dt_ms": 0.969, "distinct_values": 726, "wraps": 6}
- **u32**: {"samples": 1020, "achieved_hz": 172.4, "median_dt_ms": 0.966, "distinct_values": 69, "wraps": 0}
- **pad1**: {"samples": 1020, "achieved_hz": 172.4, "median_dt_ms": 0.972, "distinct_values": 12, "wraps": 1}
- **pad2**: {"samples": 1020, "achieved_hz": 172.4, "median_dt_ms": 0.969, "distinct_values": 60, "wraps": 0}

## S4 stepline mid-stream
- stepline while streaming: 580.3 ms (stopped: loop); stream after resume: {"u16": {"samples": 2250, "achieved_hz": 757.8, "median_dt_ms": 0.963, "distinct_values": 875, "wraps": 3}}

