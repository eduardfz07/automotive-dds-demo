"""
generate_sample_data.py - Pre-recorded Sample Data Generator
=============================================================
Generates realistic synthetic metrics CSV files for 5, 10, 20 ECU scenarios.
These files power the visualizations without requiring a full demo run.

Data models:
  DDS latency:  base 2ms + 0.5ms/ECU + gaussian noise(0, 0.8ms)
  CAN latency:  base 10ms + 2ms/ECU + bus_contention + gaussian noise(0, 2ms)
                bus_contention = 5ms × max(0, (bus_load-50)/50)
  Jitter:       DDS ≈ 2-5ms; CAN ≈ 15-40ms (arbitration variance)

All data is deterministic-seeded for reproducible demos.
"""
from __future__ import annotations

import csv
import math
import os
import random
import time
from typing import List

# Fix random seed for reproducible demo data
random.seed(42)

# ── OTA state constants ──────────────────────────────────────────────────────
STATES = ["IDLE", "DOWNLOADING", "VERIFYING", "INSTALLING", "REBOOTING", "DONE"]

# Duration budget (seconds) for each state phase (realistic ECU benchmarks)
STATE_DURATIONS = {
    "IDLE":        0.0,
    "DOWNLOADING": (2.0, 4.0),    # 2-4s for 10 MB image
    "VERIFYING":   (1.0, 2.0),    # SHA-256 check
    "INSTALLING":  (1.0, 2.0),    # Flash write
    "REBOOTING":   (0.5, 1.0),    # Power cycle
    "DONE":        0.0,
}


def _dds_latency(num_ecus: int) -> float:
    """Model DDS end-to-end latency in ms."""
    base    = 2.0
    per_ecu = 0.5
    noise   = random.gauss(0, 0.8)
    return max(0.5, base + per_ecu * num_ecus + noise)


def _can_latency(num_ecus: int) -> float:
    """Model CAN bus latency in ms with contention."""
    base        = 10.0
    per_ecu     = 2.0
    # CAN bus load grows with ECU count
    bus_load_pct = min(100, 5 * num_ecus)
    contention  = 5.0 * max(0.0, (bus_load_pct - 50) / 50.0)
    noise       = random.gauss(0, 2.0)
    return max(5.0, base + per_ecu * num_ecus + contention + noise)


def _generate_ecu_timeline(ecu_id: str, num_ecus: int,
                            start_offset_s: float = 0.0) -> List[dict]:
    """
    Generate realistic state transition timeline for one ECU.
    Returns list of row dicts for the CSV.
    """
    rows = []
    t = start_offset_s  # seconds since scenario start
    prev_state = "IDLE"

    for state in STATES[1:]:  # skip IDLE (it's the start)
        # Simulate per-ECU jitter in state transitions (gaussian, 35ms mean)
        jitter_s  = random.gauss(0.035, 0.010)
        dur_range = STATE_DURATIONS.get(state, (0.5, 1.0))
        if isinstance(dur_range, tuple):
            duration = random.uniform(*dur_range) + jitter_s
        else:
            duration = jitter_s

        t += max(0.0, duration)

        row = {
            "ecu_id":     ecu_id,
            "old_state":  prev_state,
            "new_state":  state,
            "timestamp":  round(t, 6),
            "latency_ms": round(_dds_latency(num_ecus), 3) if state == "DONE" else "",
        }
        rows.append(row)
        prev_state = state

    return rows


def generate_scenario(num_ecus: int, output_dir: str = "data/") -> str:
    """
    Generate a full scenario CSV for num_ecus ECUs.
    Returns filename written.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, f"sample_{num_ecus}ecus.csv")

    # Command is broadcast at t=0; ECUs start processing after DDS delivery (~2ms)
    command_ts = time.time()

    all_rows: List[dict] = []
    for i in range(1, num_ecus + 1):
        ecu_id = f"ECU_{i:03d}"
        # Each ECU gets a tiny start offset (DDS delivery latency simulation)
        offset = _dds_latency(num_ecus) / 1000.0  # convert ms → s
        rows   = _generate_ecu_timeline(ecu_id, num_ecus, start_offset_s=offset)
        all_rows.extend(rows)

    # Collect done latencies for jitter calculation
    done_latencies = [
        float(r["latency_ms"])
        for r in all_rows
        if r["new_state"] == "DONE" and r["latency_ms"] != ""
    ]
    jitter_ms   = round(max(done_latencies) - min(done_latencies), 3) if len(done_latencies) > 1 else 0.0
    e2e_ms      = round(max(done_latencies), 3) if done_latencies else 0.0
    throughput  = round(len(all_rows) / (e2e_ms / 1000.0), 2) if e2e_ms > 0 else 0.0

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "scenario", "num_ecus", "ecu_id", "old_state", "new_state",
            "timestamp", "latency_ms", "jitter_ms", "throughput_mps",
            "end_to_end_ms", "sequence_number",
        ])
        seq = 1
        for row in sorted(all_rows, key=lambda r: r["timestamp"]):
            writer.writerow([
                f"dds_{num_ecus}ecus",
                num_ecus,
                row["ecu_id"],
                row["old_state"],
                row["new_state"],
                row["timestamp"],
                row["latency_ms"],
                jitter_ms,
                throughput,
                e2e_ms,
                seq,
            ])
            seq += 1

    print(f"  ✓  {filename}  ({num_ecus} ECUs | E2E: {e2e_ms:.1f}ms | "
          f"Jitter: {jitter_ms:.1f}ms | {len(all_rows)} events)")
    return filename


def generate_scalability_comparison(output_dir: str = "data/") -> str:
    """
    Generate DDS vs CAN scalability comparison data.
    Models both protocols across 5-100 ECUs.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, "scalability_comparison.csv")

    ecu_counts = [5, 10, 20, 50, 100]

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "num_ecus",
            "dds_latency_ms", "can_latency_ms",
            "dds_bus_load_pct", "can_bus_load_pct",
            "dds_frames", "can_frames",
            "dds_jitter_ms", "can_jitter_ms",
            "can_congestion_factor",
        ])

        for n in ecu_counts:
            # DDS model
            dds_lat   = round(2.0 + 0.5 * n + random.gauss(0, 0.5), 2)
            dds_load  = round((n * 30 * 8) / (1000e6 * 10) * 100, 4)  # negligible
            dds_jit   = round(random.gauss(3.0, 0.5), 2)
            dds_frames = n * 7  # status samples

            # CAN model — command delivery is O(N) unicast (serial bus)
            # Each ECU requires N × 7 frames to receive the command
            bits_per_frame = 65 + 8 * 8  # 129 bits at 1 Mbps = 129 μs/frame
            frame_ms = 129 / 1e3  # ms
            base_can_lat = n * 7 * frame_ms  # serial unicast to N ECUs
            bus_load  = min(100, (n * 7 * 7 + n) * frame_ms / 10_000 * 100)  # % in 10s
            contention = 1.0 + max(0, (bus_load - 50) / 50.0)
            can_lat   = round(base_can_lat * contention + random.gauss(0, 2.0), 2)
            can_load  = round(bus_load + random.gauss(0, 1.0), 1)
            can_jit   = round(max(5.0, 5.0 + 1.5 * n + random.gauss(0, 3.0)), 2)
            can_frames = n * 7 * 7  # 7 states × 7 CAN frames each
            congestion = round(contention, 3)

            writer.writerow([
                n,
                dds_lat, max(5.0, can_lat),
                dds_load, min(100.0, can_load),
                dds_frames, can_frames,
                dds_jit, min(200.0, can_jit),
                congestion,
            ])

    print(f"  ✓  {filename}  ({len(ecu_counts)} ECU counts, DDS vs CAN)")
    return filename


def generate_qos_comparison(output_dir: str = "data/") -> str:
    """
    Generate QoS comparison data: RELIABLE vs BEST_EFFORT across ECU counts.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, "qos_comparison.csv")

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "qos", "num_ecus", "end_to_end_ms", "jitter_ms",
            "message_loss_pct", "error_count", "completed",
        ])

        for n in [5, 10, 20]:
            # RELIABLE QoS — deterministic delivery
            rel_lat   = round(2.0 + 0.5 * n + random.gauss(0, 0.5), 2)
            rel_jitter = round(random.gauss(3.0, 0.5), 2)
            writer.writerow([
                "RELIABLE", n, rel_lat, rel_jitter, 0.0, 0, 1
            ])

            # BEST_EFFORT QoS — ~3% drop rate, higher jitter, possible errors
            be_lat    = round(1.8 + 0.4 * n + random.gauss(0, 1.5), 2)
            be_jitter = round(random.gauss(12.0, 3.0), 2)
            be_loss   = round(random.gauss(3.0, 0.5), 2)
            be_errors = 1 if n >= 10 and random.random() < 0.4 else 0
            writer.writerow([
                "BEST_EFFORT", n, be_lat, be_jitter,
                max(0.5, be_loss), be_errors, 1 if n < 10 else (0 if be_errors else 1)
            ])

    print(f"  ✓  {filename}  (RELIABLE vs BEST_EFFORT, 3 ECU counts)")
    return filename


def generate_latency_distribution(output_dir: str = "data/") -> str:
    """
    Generate per-ECU latency samples for violin/box plot visualization.
    Models 50 repeated OTA cycles per ECU for statistical depth.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, "latency_distribution.csv")

    num_ecus   = 5
    num_samples = 50  # 50 OTA cycles per ECU (pre-recorded test data)

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ecu_id", "qos", "latency_ms", "run_id"])

        for run in range(num_samples):
            for i in range(1, num_ecus + 1):
                ecu_id = f"ECU_{i:03d}"
                # RELIABLE: tight distribution around 10-15s (full OTA cycle)
                rel_lat = round(
                    10000.0 + random.gauss(500.0 * i, 200.0) + random.gauss(0, 100.0),
                    1,
                )
                # BEST_EFFORT: wider distribution due to retries
                be_lat  = round(
                    9500.0 + random.gauss(600.0 * i, 500.0) + random.gauss(0, 400.0),
                    1,
                )
                writer.writerow([ecu_id, "RELIABLE",   max(5000.0, rel_lat), run])
                writer.writerow([ecu_id, "BEST_EFFORT", max(4000.0, be_lat),  run])

    print(f"  ✓  {filename}  ({num_ecus} ECUs × {num_samples} runs × 2 QoS)")
    return filename


def generate_state_timeline(output_dir: str = "data/") -> str:
    """
    Generate Gantt-chart-ready state timeline data for 5 ECUs.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, "state_timeline.csv")

    num_ecus = 5
    t0 = 0.0  # start at t=0

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ecu_id", "state", "start_s", "end_s", "duration_s"])

        for i in range(1, num_ecus + 1):
            ecu_id = f"ECU_{i:03d}"
            t = t0 + _dds_latency(num_ecus) / 1000.0  # offset by DDS delivery

            prev_state = "IDLE"
            # Add initial IDLE period
            writer.writerow([ecu_id, "IDLE", round(t0, 4), round(t, 4), round(t - t0, 4)])

            for state in ["DOWNLOADING", "VERIFYING", "INSTALLING", "REBOOTING", "DONE"]:
                jitter  = random.gauss(0.035, 0.010)
                dur_range = STATE_DURATIONS.get(state, (0.5, 1.0))
                if isinstance(dur_range, tuple):
                    duration = random.uniform(*dur_range) + jitter
                elif state == "DONE":
                    duration = 0.1
                else:
                    duration = jitter

                start = t
                end   = t + max(0.05, duration)
                writer.writerow([ecu_id, state, round(start, 4), round(end, 4),
                                  round(end - start, 4)])
                t = end

    print(f"  ✓  {filename}  ({num_ecus} ECUs Gantt timeline)")
    return filename


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Generating Sample Data for Automotive DDS Demo")
    print("=" * 60 + "\n")

    os.makedirs("data", exist_ok=True)

    print("  Generating scenario CSVs...")
    generate_scenario(5)
    generate_scenario(10)
    generate_scenario(20)

    print("\n  Generating comparison datasets...")
    generate_scalability_comparison()
    generate_qos_comparison()
    generate_latency_distribution()
    generate_state_timeline()

    print("\n" + "=" * 60)
    print("  Sample data generation complete.")
    print("  Files written to: data/")
    print("=" * 60)
    print("""
  Next steps:
    python run_demo.py --num-ecus 5 --show-comparison
    python visualize_results.py --generate-sample --show
""")
