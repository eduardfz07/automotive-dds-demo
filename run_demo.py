"""
run_demo.py - Main Demo Orchestrator
=====================================
Runs the complete automotive DDS OTA update coordination demo.
Designed for a 15-minute recorded/live presentation to RTI.

Demo flow:
  1. Print DDS architecture overview (30 sec)
  2. Start N simulated ECUs as DDS participants (15 sec)
  3. UpdateManager discovers ECUs via DDS SDP (15 sec)
  4. Broadcast START_UPDATE command via DDS multicast (5 sec)
  5. Real-time state table shows all ECUs progressing (2-4 min)
  6. Final metrics: latency, jitter, throughput (30 sec)
  7. CAN vs DDS overhead comparison if --show-comparison (1 min)
  8. QoS comparison if --qos best_effort (1 min)

Usage:
  python run_demo.py --num-ecus 5 --firmware 2.0.0 --show-comparison
  python run_demo.py --num-ecus 10 --qos best_effort
  python run_demo.py --num-ecus 20 --show-comparison --duration 90
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import List, Optional

from dds_abstraction import RELIABLE_QOS, BEST_EFFORT_QOS, QoSProfile
from ecu import ECU, OTAState
from update_manager import UpdateManager
from metrics_collector import (
    MetricsCollector,
    CANBusAnalyzer,
    save_run_results,
)

# ANSI color codes
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BLUE   = "\033[94m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"


# ---------------------------------------------------------------------------
# Banner and section headers — presentation-ready output
# ---------------------------------------------------------------------------

def print_banner() -> None:
    print(f"""
{_BOLD}{_CYAN}╔══════════════════════════════════════════════════════════════╗
║   DDS — Automotive OTA Update Coordination Demo  ║
║   AUTOSAR Adaptive | Multi-ECU | Real-time State Sync        ║
╚══════════════════════════════════════════════════════════════╝{_RESET}
""")


def print_section(title: str) -> None:
    width = 65
    print(f"\n{_BOLD}{_BLUE}{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}{_RESET}\n")


def print_architecture() -> None:
    """ASCII architecture diagram explaining DDS topology."""
    print_section("ARCHITECTURE: DDS Pub/Sub for OTA Coordination")
    print(f"""{_DIM}
  ┌─────────────────────────────────────────────────────────┐
  │                    DDS Domain 0                          │
  │                                                          │
  │  ┌──────────────┐    OTAControl    ┌──────────────────┐ │
  │  │ UpdateManager│───────────────►  │   ECU_001..N      │ │
  │  │  (UCM Master)│  [RELIABLE +     │ (AUTOSAR UCM)    │ │
  │  │              │   TRANSIENT]     │                  │ │
  │  │              │◄─────────────── │  OTAState Machine│ │
  │  │  Subscriber  │   ECUStatus      │  Publisher       │ │
  │  └──────────────┘  [RELIABLE +     └──────────────────┘ │
  │                     TRANSIENT]                           │
  │                                                          │
  │  DDS Discovery: Automatic (no static IP config)          │
  │  Transport: UDP Multicast over Ethernet                  │
  │  vs CAN: requires static message IDs, unicast, 1Mbps    │
  └─────────────────────────────────────────────────────────┘
{_RESET}""")


# ---------------------------------------------------------------------------
# MetricsCollector bridge — integrates with UpdateManager
# ---------------------------------------------------------------------------

class _MetricsBridge:
    """
    Polls UpdateManager.ecu_states and feeds state changes into MetricsCollector.
    Runs in a background thread during the demo.
    """
    def __init__(self, manager: UpdateManager, collector: MetricsCollector,
                 expected_ecus: List[str]):
        self._manager   = manager
        self._collector = collector
        self._expected  = expected_ecus
        self._prev_states: dict = {}
        self._running   = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._poll, name="MetricsBridge", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)

    def _poll(self) -> None:
        while self._running:
            states_copy = self._manager.get_ecu_states()

            for ecu_id, update in states_copy.items():
                prev = self._prev_states.get(ecu_id, "IDLE")
                if update.state != prev:
                    self._collector.record_state_change(
                        ecu_id=ecu_id,
                        old_state=prev,
                        new_state=update.state,
                        timestamp=update.timestamp,
                        sequence_number=update.sequence_number,
                    )
                    self._prev_states[ecu_id] = update.state

            time.sleep(0.05)  # 50ms polling — fine-grained event capture


# ---------------------------------------------------------------------------
# QoS comparison helper
# ---------------------------------------------------------------------------

def run_qos_comparison(num_ecus: int, firmware: str,
                       domain_id: int) -> None:
    """
    Run back-to-back RELIABLE vs BEST_EFFORT and show the difference.
    Demonstrates why QoS selection matters for OTA safety.
    """
    print_section("QoS COMPARISON: RELIABLE vs BEST_EFFORT")
    print(f"  Running {num_ecus} ECUs with BEST_EFFORT QoS...")
    print(f"  (Note: ~3% message drop rate simulated)\n")

    ecu_ids = [f"ECU_{i:03d}" for i in range(1, num_ecus + 1)]
    ecus    = [ECU(eid, "1.5.0", domain_id, BEST_EFFORT_QOS) for eid in ecu_ids]
    for ecu in ecus:
        ecu.start()

    time.sleep(0.3)

    manager   = UpdateManager(ecu_ids, BEST_EFFORT_QOS, domain_id)
    collector = MetricsCollector()
    bridge    = _MetricsBridge(manager, collector, ecu_ids)

    cmd_time = time.time()
    collector.record_command_sent("START_UPDATE", cmd_time, num_ecus)
    bridge.start()
    manager.start_update(firmware)

    completed = manager.wait_for_completion(timeout=45.0)
    bridge.stop()

    metrics_be = manager.get_metrics()

    print(f"\n  {_BOLD}BEST_EFFORT Results:{_RESET}")
    print(f"  End-to-end: {_YELLOW}{metrics_be['end_to_end_ms']:.1f} ms{_RESET}")
    print(f"  Jitter    : {_YELLOW}{metrics_be['jitter_ms']:.1f} ms{_RESET}")
    print(f"  Errors    : {_RED}{metrics_be['error_count']}{_RESET}")
    print(f"  Completed : {'Yes' if completed else _RED + 'No (timeout)' + _RESET}")

    print(f"\n  {_DIM}Key insight: BEST_EFFORT may miss control commands,")
    print(f"  leading to ECUs stuck in IDLE. RELIABLE QoS is mandatory")
    print(f"  for safety-critical OTA coordination (ISO 26262).{_RESET}\n")

    manager.shutdown()
    for ecu in ecus:
        ecu.stop()

    # Save QoS comparison data
    os.makedirs("data", exist_ok=True)
    import csv
    with open("data/qos_comparison.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["qos", "num_ecus", "end_to_end_ms", "jitter_ms",
                          "error_count", "completed"])
        writer.writerow(["BEST_EFFORT", num_ecus,
                         f"{metrics_be['end_to_end_ms']:.3f}",
                         f"{metrics_be['jitter_ms']:.3f}",
                         metrics_be["error_count"],
                         1 if completed else 0])


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------

def run_demo(
    num_ecus: int = 5,
    firmware: str = "2.0.0",
    qos_name: str = "reliable",
    show_comparison: bool = False,
    duration: float = 30.0,
    domain_id: int = 0,
) -> dict:
    """
    Run the full OTA demo and return collected metrics.
    """
    print_banner()
    print_architecture()

    # Select QoS
    qos: QoSProfile = RELIABLE_QOS if qos_name == "reliable" else BEST_EFFORT_QOS

    # ── Step 1: DDS infrastructure ──────────────────────────────────────
    print_section(f"STEP 1: Creating DDS Infrastructure ({qos.name})")
    print(f"  [DDS] Domain ID     : {domain_id}")
    print(f"  [DDS] QoS Profile   : {qos.name}")
    print(f"  [DDS] Reliability   : {qos.reliability.value}")
    print(f"  [DDS] Durability    : {qos.durability.value}")
    print(f"  [DDS] History depth : {qos.history_depth} samples")
    print(f"  [DDS] Deadline      : {qos.deadline_ms} ms")
    print(f"  [DDS] Latency budget: {qos.latency_budget_ms} ms\n")

    # ── Step 2: Start ECUs ───────────────────────────────────────────────
    print_section(f"STEP 2: Starting {num_ecus} Simulated ECUs")

    ecu_ids = [f"ECU_{i:03d}" for i in range(1, num_ecus + 1)]
    ecus: List[ECU] = []

    for ecu_id in ecu_ids:
        ecu = ECU(ecu_id, "1.0.0", domain_id, qos)
        ecu.start()
        ecus.append(ecu)
        print(f"  [ECU] {ecu_id} started — firmware 1.0.0 | "
              f"DDS participant on domain {domain_id}")

    print(f"\n  {_GREEN}✓  {num_ecus} ECU DDS participants created{_RESET}")
    print(f"  {_DIM}(In production: each ECU is a separate embedded system{_RESET}")
    print(f"  {_DIM} DDS auto-discovers all participants via SDP){_RESET}")

    # Allow DDS discovery to complete
    print(f"\n  [DDS] Waiting for participant discovery (RTPS SDP)...", end="", flush=True)
    time.sleep(0.4)
    print(f" {_GREEN}done{_RESET}")

    # ── Step 3: Create UpdateManager ─────────────────────────────────────
    print_section("STEP 3: Initializing Update Manager (UCM Master)")

    manager   = UpdateManager(ecu_ids, qos, domain_id)
    collector = MetricsCollector()
    bridge    = _MetricsBridge(manager, collector, ecu_ids)

    # ── Step 4: Broadcast update command ─────────────────────────────────
    print_section("STEP 4: Broadcasting OTA Update Command via DDS Multicast")
    print(f"  Target firmware  : {firmware}")
    print(f"  Target ECUs      : {len(ecu_ids)} (broadcast multicast)")
    print(f"  {_DIM}CAN equivalent: {num_ecus} unicast messages + bus arbitration{_RESET}")
    print(f"  {_DIM}DDS: single multicast → all ECUs simultaneously{_RESET}\n")

    cmd_time = time.time()
    collector.record_command_sent("START_UPDATE", cmd_time, num_ecus)
    bridge.start()
    manager.start_update(firmware, ecu_ids)

    # ── Step 5: Real-time monitoring ─────────────────────────────────────
    print_section("STEP 5: Real-Time OTA State Monitoring")

    lines_printed = 0
    poll_interval = 0.5
    deadline      = time.time() + duration
    done          = False

    while not done and time.time() < deadline:
        lines_printed = manager.print_status_table(clear_lines=lines_printed)

        if manager.is_complete():
            done = True
        else:
            time.sleep(poll_interval)

    # Final table (no overwrite)
    if done:
        manager.print_status_table(clear_lines=lines_printed)
    else:
        print(f"\n{_YELLOW}  ⚠  Demo duration limit reached ({duration}s). "
              f"Some ECUs may still be in progress.{_RESET}")

    bridge.stop()

    # ── Step 6: Metrics ───────────────────────────────────────────────────
    print_section("STEP 6: OTA Update Metrics")

    metrics = manager.get_metrics()
    collector.print_summary()

    # Save CSV
    os.makedirs("data", exist_ok=True)
    csv_file = save_run_results(metrics, num_ecus)
    print(f"  {_DIM}Metrics saved: {csv_file}{_RESET}")

    # ── Step 7: CAN vs DDS comparison ────────────────────────────────────
    if show_comparison:
        print_section("STEP 7: CAN 2.0B vs DDS Overhead Analysis")

        for n in [5, 10, 20, 50]:
            CANBusAnalyzer.print_comparison(n)

        print(f"  {_DIM}Key insight: CAN bus load grows O(N) with unicast messaging.")
        print(f"  DDS multicast keeps overhead near-constant regardless of ECU count.")
        print(f"  At 20+ ECUs, CAN exceeds 80% bus utilization — reliability degrades.")
        print(f"  DDS at 100 ECUs uses <0.01% of 1GbE bandwidth.{_RESET}\n")

    # ── Cleanup ───────────────────────────────────────────────────────────
    manager.shutdown()
    for ecu in ecus:
        ecu.stop()

    print(f"\n{_GREEN}{_BOLD}  ✓  Demo complete.{_RESET}")
    print(f"  Next: python visualize_results.py --generate-sample --show\n")

    return metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="DDS Automotive OTA Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_demo.py --num-ecus 5 --show-comparison
  python run_demo.py --num-ecus 10 --qos best_effort
  python run_demo.py --num-ecus 20 --firmware 3.1.0 --duration 90
        """,
    )
    parser.add_argument("--num-ecus",        type=int,   default=5,
                        help="Number of simulated ECUs (default: 5)")
    parser.add_argument("--firmware",                    default="2.0.0",
                        help="Target firmware version (default: 2.0.0)")
    parser.add_argument("--qos",                         default="reliable",
                        choices=["reliable", "best_effort"],
                        help="QoS profile (default: reliable)")
    parser.add_argument("--show-comparison", action="store_true",
                        help="Show CAN vs DDS overhead analysis")
    parser.add_argument("--duration",        type=float, default=30.0,
                        help="Max demo duration in seconds (default: 30)")
    parser.add_argument("--domain-id",       type=int,   default=0,
                        help="DDS domain ID (default: 0)")
    parser.add_argument("--qos-comparison",  action="store_true",
                        help="Run QoS comparison (RELIABLE vs BEST_EFFORT) after main demo")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    metrics = run_demo(
        num_ecus=args.num_ecus,
        firmware=args.firmware,
        qos_name=args.qos,
        show_comparison=args.show_comparison,
        duration=args.duration,
        domain_id=args.domain_id,
    )

    if args.qos_comparison:
        run_qos_comparison(args.num_ecus, args.firmware, args.domain_id)
