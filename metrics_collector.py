"""
metrics_collector.py - Metrics Collection and Analysis
=======================================================
Collects, analyzes, and exports performance metrics from the OTA demo.
Also provides CAN bus overhead analysis for the comparison slides.

Metrics captured:
  - State transition latencies per ECU
  - Command-to-acknowledgment latency (DDS discovery speed)
  - Jitter (synchronization spread across ECUs)
  - Throughput (samples/second on the DDS bus)
  - End-to-end latency (command to last DONE)

CAN Bus model (CANBusAnalyzer):
  CAN 2.0B frame structure:
    SOF(1) + Identifier(29) + Control(6) + Data(0-64 bits) +
    CRC(16) + ACK(2) + EOF(7) = ~125 bits overhead for 64-bit data frame
  For an OTA state update (~50 bytes = 400 bits):
    Requires ceil(50/8) = 7 CAN frames (8 bytes data per frame)
  At 1 Mbps with 50% efficiency: effective ~500 frames/sec capacity
  For N ECUs × 7 states × 7 frames = total frames; congestion grows O(N).
"""
from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import numpy as np
    import pandas as pd
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore
    pd = None  # type: ignore

# ANSI colors
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------

@dataclass
class StateChangeEvent:
    """A single ECU state transition event."""
    ecu_id: str
    old_state: str
    new_state: str
    timestamp: float
    sequence_number: int = 0


class MetricsCollector:
    """
    Collects and analyzes OTA update metrics.

    Usage:
        collector = MetricsCollector()
        collector.record_command_sent("START_UPDATE", time.time(), 5)
        collector.record_state_change("ECU_001", "IDLE", "DOWNLOADING", time.time())
        ...
        collector.print_summary()
        collector.export_to_csv("data/run_001.csv", num_ecus=5)
    """

    def __init__(self):
        self._events: List[StateChangeEvent] = []
        self._command_sent_time: Optional[float] = None
        self._command_name: str = ""
        self._target_count: int = 0
        self._message_count: int = 0
        self._start_time: Optional[float] = None

    def record_command_sent(self, command: str, timestamp: float,
                            target_count: int) -> None:
        """Record when the OTA command was broadcast."""
        self._command_sent_time = timestamp
        self._command_name = command
        self._target_count = target_count
        if self._start_time is None:
            self._start_time = timestamp

    def record_state_change(self, ecu_id: str, old_state: str,
                            new_state: str, timestamp: float,
                            sequence_number: int = 0) -> None:
        """Record a single ECU state transition."""
        self._events.append(StateChangeEvent(
            ecu_id=ecu_id,
            old_state=old_state,
            new_state=new_state,
            timestamp=timestamp,
            sequence_number=sequence_number,
        ))
        self._message_count += 1

    def calculate_latency(self) -> Dict[str, float]:
        """
        Per-ECU latency: time from command send to ECU reaching DONE state.
        Returns dict {ecu_id: latency_ms}.
        """
        if not self._command_sent_time:
            return {}

        latencies: Dict[str, float] = {}
        for event in self._events:
            if event.new_state == "DONE":
                lat_ms = (event.timestamp - self._command_sent_time) * 1000.0
                latencies[event.ecu_id] = lat_ms

        return latencies

    def calculate_jitter(self) -> float:
        """
        Fleet synchronization jitter: max_latency - min_latency across ECUs.
        Lower jitter means ECUs complete more simultaneously (better DDS
        multicast efficiency vs. CAN sequential polling).
        """
        latencies = list(self.calculate_latency().values())
        if len(latencies) < 2:
            return 0.0
        return max(latencies) - min(latencies)

    def calculate_throughput(self) -> float:
        """
        Message throughput: total state-change messages per second.
        This measures how fast the DDS bus conveyed ECU state information.
        """
        if not self._events or not self._start_time:
            return 0.0
        end_ts = max(e.timestamp for e in self._events)
        duration = end_ts - self._start_time
        if duration <= 0:
            return 0.0
        return self._message_count / duration

    def calculate_end_to_end_latency(self) -> float:
        """
        Total time from command sent to last ECU reaching DONE (in ms).
        This is the headline metric for demo comparison with CAN.
        """
        latencies = list(self.calculate_latency().values())
        return max(latencies) if latencies else 0.0

    def calculate_first_ack_latency(self) -> float:
        """Time from command to first ECU leaving IDLE state (ms)."""
        if not self._command_sent_time:
            return 0.0
        dl_events = [e for e in self._events if e.new_state == "DOWNLOADING"]
        if not dl_events:
            return 0.0
        first = min(e.timestamp for e in dl_events)
        return (first - self._command_sent_time) * 1000.0

    def get_state_timeline(self) -> Dict[str, List[Tuple[str, float, float]]]:
        """
        Build a Gantt-chart-ready timeline per ECU.
        Returns {ecu_id: [(state, start_time, end_time), ...]}
        """
        # Group events by ECU
        by_ecu: Dict[str, List[StateChangeEvent]] = {}
        for e in sorted(self._events, key=lambda x: x.timestamp):
            by_ecu.setdefault(e.ecu_id, []).append(e)

        timeline: Dict[str, List[Tuple[str, float, float]]] = {}
        for ecu_id, events in by_ecu.items():
            segments = []
            for i, event in enumerate(events):
                start = event.timestamp
                end = events[i + 1].timestamp if i + 1 < len(events) else start + 0.5
                segments.append((event.new_state, start, end))
            timeline[ecu_id] = segments

        return timeline

    def export_to_csv(self, filename: str, num_ecus: int,
                      scenario_label: str = "") -> None:
        """
        Export detailed metrics to CSV for visualization and analysis.

        CSV columns:
          scenario, num_ecus, ecu_id, state, timestamp, latency_ms,
          jitter_ms, throughput_mps, end_to_end_ms
        """
        os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else ".", exist_ok=True)

        latencies = self.calculate_latency()
        jitter    = self.calculate_jitter()
        throughput = self.calculate_throughput()
        e2e       = self.calculate_end_to_end_latency()

        with open(filename, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "scenario", "num_ecus", "ecu_id", "old_state", "new_state",
                "timestamp", "latency_ms", "jitter_ms", "throughput_mps",
                "end_to_end_ms", "sequence_number",
            ])
            for event in self._events:
                lat = latencies.get(event.ecu_id, 0.0) if event.new_state == "DONE" else ""
                writer.writerow([
                    scenario_label or f"{num_ecus}ecus",
                    num_ecus,
                    event.ecu_id,
                    event.old_state,
                    event.new_state,
                    f"{event.timestamp:.6f}",
                    f"{lat:.3f}" if lat != "" else "",
                    f"{jitter:.3f}",
                    f"{throughput:.3f}",
                    f"{e2e:.3f}",
                    event.sequence_number,
                ])

    def print_summary(self) -> None:
        """Print a colored terminal summary of collected metrics."""
        latencies  = self.calculate_latency()
        jitter     = self.calculate_jitter()
        throughput = self.calculate_throughput()
        e2e        = self.calculate_end_to_end_latency()
        first_ack  = self.calculate_first_ack_latency()

        lat_values = list(latencies.values())
        avg_lat    = sum(lat_values) / len(lat_values) if lat_values else 0.0

        print(f"\n{_BOLD}{'─'*55}")
        print("  DDS OTA METRICS SUMMARY")
        print(f"{'─'*55}{_RESET}")
        print(f"  {'Command send → first ACK:':<32} {_CYAN}{first_ack:>8.1f} ms{_RESET}")
        print(f"  {'End-to-end latency (E2E):':<32} {_GREEN}{e2e:>8.1f} ms{_RESET}")
        print(f"  {'Average per-ECU latency:':<32} {_GREEN}{avg_lat:>8.1f} ms{_RESET}")
        print(f"  {'Fleet sync jitter:':<32} {_YELLOW}{jitter:>8.1f} ms{_RESET}")
        print(f"  {'DDS throughput:':<32} {_CYAN}{throughput:>8.1f} msg/s{_RESET}")
        print(f"  {'Total state events:':<32} {self._message_count:>8}")
        print(f"{_BOLD}{'─'*55}{_RESET}\n")

        if latencies:
            print(f"  {_BOLD}Per-ECU Latency:{_RESET}")
            for ecu_id, lat in sorted(latencies.items()):
                bar_len = int(lat / max(lat_values) * 30)
                bar = "█" * bar_len
                print(f"    {ecu_id:<12} {_GREEN}{lat:>8.1f} ms{_RESET}  {_GREEN}{bar}{_RESET}")
            print()


# ---------------------------------------------------------------------------
# CAN Bus Analyzer — static comparison methods
# ---------------------------------------------------------------------------

class CANBusAnalyzer:
    """
    Models CAN 2.0B bus overhead for OTA coordination at scale.

    CAN 2.0B frame structure (extended ID):
      Start of Frame:    1 bit
      Identifier:       29 bits (extended)
      RTR/IDE/r0:        3 bits
      DLC:               4 bits
      Data:            0–64 bits (0–8 bytes)
      CRC:              16 bits
      ACK:               2 bits
      EOF + IFS:        10 bits
      Total overhead:  ~65 bits + data bits

    For an OTA state update (ecu_id 8B + state 4B + progress 4B +
    timestamp 8B + seq 4B + version 8B + error 4B = ~50 bytes):
      - 50 bytes ÷ 8 bytes/frame = 7 CAN frames required
      - Each frame = 65 + 64 = 129 bits at 1 Mbps = 129 μs
      - 7 frames = ~903 μs per ECU state update

    Comparison point:
      DDS over Ethernet uses efficient CDR serialization (~30 bytes per
      OTAStatus sample), UDP multicast (one packet reaches all ECUs),
      and Gigabit bandwidth — making it orders of magnitude more efficient
      for N>5 ECUs.
    """

    # CAN 2.0B constants
    CAN_DATA_BYTES_PER_FRAME  = 8
    CAN_FRAME_OVERHEAD_BITS   = 65   # overhead bits per frame
    CAN_BIT_RATE_MBPS         = 1.0
    OTA_UPDATE_BYTES          = 50   # bytes per OTA state update message
    CAN_BUS_UTILIZATION_LIMIT = 0.80 # 80% max before congestion

    # DDS constants
    DDS_SAMPLE_BYTES          = 30   # CDR-serialized OTA sample
    DDS_ETHERNET_MBPS         = 1000 # 1 Gbps
    DDS_MULTICAST_OVERHEAD    = 1.0  # multicast: 1 transmission for N receivers

    @staticmethod
    def calculate_can_overhead(num_ecus: int,
                               num_states: int = 7) -> dict:
        """
        Calculate CAN bus overhead for OTA coordination.

        Model:
          - Each ECU emits num_states state updates during an OTA cycle.
          - Each update requires 7 CAN frames.
          - Plus the OTA command: 1 frame × N ECUs (unicast).
          - At 1 Mbps, effective throughput ≈ 500 frames/sec (50% efficiency).

        Returns dict with: frames_total, bus_load_pct, estimated_latency_ms,
                           congestion_factor, messages_per_ecu
        """
        frames_per_update = math.ceil(
            CANBusAnalyzer.OTA_UPDATE_BYTES / CANBusAnalyzer.CAN_DATA_BYTES_PER_FRAME
        )
        status_frames  = num_ecus * num_states * frames_per_update
        # CAN command delivery is UNICAST: the UpdateManager must send the
        # START_UPDATE message to each ECU individually → N sequential transmissions.
        # This is the critical O(N) bottleneck vs DDS O(1) multicast.
        command_frames = num_ecus * frames_per_update
        frames_total   = status_frames + command_frames

        # Frame duration at 1 Mbps
        bits_per_frame = (
            CANBusAnalyzer.CAN_FRAME_OVERHEAD_BITS
            + CANBusAnalyzer.CAN_DATA_BYTES_PER_FRAME * 8
        )
        frame_duration_s = bits_per_frame / (CANBusAnalyzer.CAN_BIT_RATE_MBPS * 1e6)
        bus_time_s = frames_total * frame_duration_s

        # Window: 10-second OTA cycle
        window_s = 10.0
        bus_load_pct = (bus_time_s / window_s) * 100.0

        # Congestion factor: increases queuing delay when bus_load > 50%
        if bus_load_pct > 50:
            congestion_factor = 1.0 + (bus_load_pct - 50) / 50.0
        else:
            congestion_factor = 1.0

        # CAN command latency: manager must unicast to each ECU serially.
        # Last ECU only receives command after (N-1) other transmissions complete.
        # base = N × frames_per_update × frame_duration (serial unicast delivery)
        base_latency_ms      = num_ecus * frames_per_update * frame_duration_s * 1000.0
        estimated_latency_ms = base_latency_ms * congestion_factor

        return {
            "frames_per_update":      frames_per_update,
            "frames_total":           frames_total,
            "bus_load_pct":           min(bus_load_pct, 100.0),
            "estimated_latency_ms":   estimated_latency_ms,
            "congestion_factor":      congestion_factor,
            "messages_per_ecu":       num_states * frames_per_update,
            "supports_multicast":     False,
            "protocol":               "CAN 2.0B @ 1Mbps",
        }

    @staticmethod
    def calculate_dds_overhead(num_ecus: int,
                               num_states: int = 7) -> dict:
        """
        Calculate DDS overhead for OTA coordination.

        Model:
          - DDS CDR serialization: ~30 bytes per OTA sample.
          - UDP multicast: 1 network transmission for all N receivers.
          - Gigabit Ethernet: 1000 Mbps.
          - DDS discovery adds ~50 bytes/participant at startup only.
          - No per-ECU unicast needed for commands (true multicast).

        Returns comparable metrics to calculate_can_overhead.
        """
        bytes_per_sample = CANBusAnalyzer.DDS_SAMPLE_BYTES
        # Header: UDP(8) + IP(20) + Ethernet(14) + RTPS(20) = 62 bytes
        network_overhead_bytes = 62

        total_bytes_per_status = bytes_per_sample + network_overhead_bytes

        # Multicast: 1 transmission per status update regardless of N ECUs
        status_samples     = num_ecus * num_states  # each ECU sends N state updates
        command_samples    = 1                       # 1 multicast command reaches all ECUs

        total_bytes = (
            status_samples * total_bytes_per_status
            + command_samples * total_bytes_per_status
        )

        # DDS latency model: base 2ms + 0.1ms per ECU (multicast fanout overhead)
        base_latency_ms    = 2.0
        per_ecu_latency_ms = 0.1
        estimated_latency_ms = base_latency_ms + per_ecu_latency_ms * num_ecus

        # Bus load on Gigabit Ethernet (effectively negligible)
        window_s = 10.0
        bits_total = total_bytes * 8
        bus_load_pct = (bits_total / (CANBusAnalyzer.DDS_ETHERNET_MBPS * 1e6 * window_s)) * 100.0

        return {
            "bytes_per_sample":       bytes_per_sample,
            "total_bytes":            total_bytes,
            "samples_total":          status_samples + command_samples,
            "bus_load_pct":           bus_load_pct,
            "estimated_latency_ms":   estimated_latency_ms,
            "congestion_factor":      1.0,
            "supports_multicast":     True,
            "protocol":               "DDS/RTPS over GbE",
        }

    @staticmethod
    def print_comparison(num_ecus: int) -> None:
        """Print a formatted CAN vs DDS comparison table."""
        can = CANBusAnalyzer.calculate_can_overhead(num_ecus)
        dds_m = CANBusAnalyzer.calculate_dds_overhead(num_ecus)

        print(f"\n{_BOLD}{'─'*65}")
        print(f"  CAN 2.0B vs RTI Connext DDS — {num_ecus} ECUs")
        print(f"{'─'*65}{_RESET}")
        print(f"  {'Metric':<35} {'CAN 2.0B':>12} {'DDS/RTPS':>12}")
        print(f"  {'─'*35} {'─'*12} {'─'*12}")

        metrics = [
            ("Est. latency (ms)",        f"{can['estimated_latency_ms']:>10.1f}",
                                          f"{dds_m['estimated_latency_ms']:>10.1f}"),
            ("Bus load (%)",              f"{can['bus_load_pct']:>10.1f}",
                                          f"{dds_m['bus_load_pct']:>10.3f}"),
            ("Frames/msgs per update",   f"{can['frames_per_update']:>10}",
                                          f"{dds_m['bytes_per_sample']:>9}B"),
            ("Multicast support",         f"{'No':>12}",  f"{'Yes':>12}"),
            ("Congestion factor",         f"{can['congestion_factor']:>10.2f}x",
                                          f"{dds_m['congestion_factor']:>10.2f}x"),
        ]

        for label, can_val, dds_val in metrics:
            print(f"  {label:<35} {_RED}{can_val}{_RESET} {_GREEN}{dds_val}{_RESET}")

        print(f"{_BOLD}{'─'*65}{_RESET}\n")


# ---------------------------------------------------------------------------
# Scalability data generator
# ---------------------------------------------------------------------------

def generate_scalability_data(
    ecu_counts: Optional[List[int]] = None,
) -> Optional[object]:
    """
    Generate modeled scalability comparison data (DDS vs CAN) as a DataFrame.
    Returns a pandas DataFrame or None if pandas is not available.
    """
    if not _NUMPY_AVAILABLE:
        return None

    counts = ecu_counts or [5, 10, 20, 50, 100]
    rows = []
    for n in counts:
        can_data = CANBusAnalyzer.calculate_can_overhead(n)
        dds_data = CANBusAnalyzer.calculate_dds_overhead(n)
        rows.append({
            "num_ecus":              n,
            "can_latency_ms":        can_data["estimated_latency_ms"],
            "dds_latency_ms":        dds_data["estimated_latency_ms"],
            "can_bus_load_pct":      can_data["bus_load_pct"],
            "dds_bus_load_pct":      dds_data["bus_load_pct"],
            "can_frames":            can_data["frames_total"],
            "dds_samples":           dds_data["samples_total"],
            "can_congestion_factor": can_data["congestion_factor"],
            "dds_congestion_factor": dds_data["congestion_factor"],
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Save run results to data/ directory
# ---------------------------------------------------------------------------

def save_run_results(metrics: dict, num_ecus: int,
                     output_dir: str = "data/") -> str:
    """
    Save a run's metrics to CSV file.
    Returns the filename written.
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(output_dir, f"run_{timestamp_str}.csv")

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "num_ecus", "ecu_id", "latency_ms", "jitter_ms",
            "end_to_end_ms", "qos_profile", "error_count",
        ])
        per_ecu = metrics.get("per_ecu_latency_ms", {})
        jitter  = metrics.get("jitter_ms", 0.0)
        e2e     = metrics.get("end_to_end_ms", 0.0)
        qos     = metrics.get("qos_profile", "unknown")
        errors  = metrics.get("error_count", 0)

        for ecu_id, latency in per_ecu.items():
            writer.writerow([num_ecus, ecu_id, f"{latency:.3f}",
                             f"{jitter:.3f}", f"{e2e:.3f}", qos, errors])

    return filename
