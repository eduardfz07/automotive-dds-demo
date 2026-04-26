"""
update_manager.py - OTA Update Coordinator
===========================================
Coordinates a fleet-wide OTA firmware update across multiple ECUs using DDS.

Architecture:
  UpdateManager subscribes to 'ECUStatus' (receiving state from all ECUs)
  and publishes to 'OTAControl' (broadcasting commands to all ECUs).

DDS advantages demonstrated here:
  1. Single multicast publish reaches ALL ECUs simultaneously — O(1) command
     delivery regardless of fleet size (vs. CAN unicast: O(N) messages).
  2. RELIABLE QoS: the middleware automatically retransmits if an ECU misses
     the command (e.g., briefly offline during controlled reset).
  3. TRANSIENT_LOCAL: a late-joining monitor gets the full state history.
  4. Built-in discovery: no explicit ECU address configuration required.

AUTOSAR UCM context:
  This models the UCM Master (Update and Configuration Management Master)
  which orchestrates the update sequence across Vehicle ECUs per ISO 26262.
"""
from __future__ import annotations

import argparse
import threading
import time
from typing import Dict, List, Optional

from dds_abstraction import (
    QoSProfile,
    RELIABLE_QOS,
    BEST_EFFORT_QOS,
    CONTROL_QOS,
    create_participant,
    create_topic,
    create_writer,
    create_reader,
    write,
    shutdown,
)
from ecu import ECU, ECUStateUpdate, OTAState, CMD_START_UPDATE


# ANSI color codes for terminal output
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

# Map OTA states to display colors
_STATE_COLORS = {
    OTAState.IDLE.value:        _CYAN,
    OTAState.DOWNLOADING.value: _YELLOW,
    OTAState.VERIFYING.value:   _YELLOW,
    OTAState.INSTALLING.value:  _YELLOW,
    OTAState.REBOOTING.value:   _YELLOW,
    OTAState.DONE.value:        _GREEN,
    OTAState.ERROR.value:       _RED,
}


class UpdateManager:
    """
    OTA Update Coordinator — the DDS-based UCM Master.

    Responsibilities:
      - Broadcast START_UPDATE command to all target ECUs via DDS multicast.
      - Monitor ECU state via 'ECUStatus' subscriptions.
      - Track per-ECU latency and overall fleet synchronization jitter.
      - Display real-time progress table in the terminal.
      - Report metrics after completion.

    Late-join / automatic discovery:
      When allow_late_join=True, the manager accepts status samples from ECUs
      that were not present at start_update time.  Because OTAControl is
      published with RELIABLE + TRANSIENT_LOCAL QoS, the middleware delivers
      the cached START_UPDATE command to the newly-joined ECU automatically —
      no explicit re-send is required.  This models AUTOSAR Adaptive's
      built-in DDS discovery (RTPS SDP) where new vehicle ECUs integrate into
      an ongoing update campaign without any static configuration.

    Thread safety:
      All access to ecu_states and metrics is protected by _lock.
      The DDS listener callback runs on a middleware thread.
    """

    def __init__(
        self,
        expected_ecus: List[str],
        qos_profile: Optional[QoSProfile] = None,
        domain_id: int = 0,
        allow_late_join: bool = False,
    ):
        self.expected_ecus = expected_ecus
        self.qos = qos_profile or RELIABLE_QOS
        self.domain_id = domain_id

        # Whether to accept and track ECUs that join after start_update
        self.allow_late_join = allow_late_join
        # ECU IDs that were discovered dynamically (not in expected_ecus)
        self.late_join_ecus: List[str] = []

        # Per-ECU state tracking {ecu_id: ECUStateUpdate}
        self.ecu_states: Dict[str, ECUStateUpdate] = {}
        # Per-ECU timestamps for each state transition {ecu_id: {state: timestamp}}
        self._state_timestamps: Dict[str, Dict[str, float]] = {
            ecu_id: {} for ecu_id in expected_ecus
        }
        # Time when the update command was broadcast
        self._command_timestamp: Optional[float] = None
        self._target_firmware: str = ""

        self._lock = threading.Lock()
        self._completion_event = threading.Event()

        # DDS setup
        self._participant = create_participant(domain_id, self.qos)
        self._status_topic  = create_topic(self._participant, "ECUStatus",  "ECUStateUpdate")
        self._control_topic = create_topic(self._participant, "OTAControl", "OTACommand")

        # DataWriter for OTA commands (RELIABLE ensures delivery to all ECUs)
        self._control_writer = create_writer(
            self._participant, self._control_topic, CONTROL_QOS
        )

        # DataReader for ECU status updates (RELIABLE + TRANSIENT_LOCAL so we
        # get history from ECUs that were already running before we started)
        self._status_reader = create_reader(
            self._participant,
            self._status_topic,
            self.qos,
            on_data_available=self._on_ecu_status,
        )

        print(f"[DDS] UpdateManager created on domain {domain_id}")
        print(f"[DDS] Topic 'ECUStatus' subscribed with {self.qos.name} QoS")
        print(f"[DDS] Topic 'OTAControl' writer ready with RELIABLE + TRANSIENT_LOCAL QoS")
        print(f"[DDS] Expecting {len(expected_ecus)} ECUs: {', '.join(expected_ecus)}\n")

    # ------------------------------------------------------------------
    # DDS listener
    # ------------------------------------------------------------------

    def _on_ecu_status(self, sample: dict) -> None:
        """
        DDS data-available callback — called by middleware on each new sample.

        Updates internal state table and records per-state timestamps for
        latency calculation. Signals completion when all ECUs reach DONE/ERROR.

        Late-join handling:
          If allow_late_join=True, status samples from ECUs not in
          expected_ecus are accepted and tracked in late_join_ecus.
          The ECU received the START_UPDATE command automatically via the
          TRANSIENT_LOCAL cache — this callback records its progress.
        """
        ecu_id = sample.get("ecu_id", "")
        if not ecu_id:
            return

        is_expected = ecu_id in self.expected_ecus
        if not is_expected:
            if not self.allow_late_join:
                return
            # Dynamically register this ECU as a late joiner (first sight only)
            with self._lock:
                if ecu_id not in self.late_join_ecus:
                    self.late_join_ecus.append(ecu_id)
                    self._state_timestamps[ecu_id] = {}

        update = ECUStateUpdate.from_dict(sample)

        with self._lock:
            self.ecu_states[ecu_id] = update
            # Record first-arrival timestamp for this state
            ts_map = self._state_timestamps.setdefault(ecu_id, {})
            state_key = update.state
            if state_key not in ts_map:
                ts_map[state_key] = update.timestamp

            # Completion is determined solely by the original expected_ecus —
            # late joiners are bonus and do not block the completion signal.
            terminal_states = {OTAState.DONE.value, OTAState.ERROR.value}
            all_terminal = len(self.expected_ecus) > 0 and all(
                self.ecu_states.get(eid) is not None
                and self.ecu_states[eid].state in terminal_states
                for eid in self.expected_ecus
            )

        if all_terminal:
            self._completion_event.set()

    # ------------------------------------------------------------------
    # Update coordination
    # ------------------------------------------------------------------

    def start_update(self, firmware_version: str,
                     target_ecus: Optional[List[str]] = None) -> None:
        """
        Broadcast START_UPDATE command to target ECUs.

        DDS multicast means this single write() call reaches ALL ECUs
        simultaneously — no per-ECU unicast loop required.
        The RELIABLE QoS guarantees delivery with automatic retransmission.

        When allow_late_join=True the command is broadcast with an empty
        target list so that the TRANSIENT_LOCAL cache can replay it to any
        ECU that subscribes to OTAControl after this call — no re-send needed.
        """
        if target_ecus is None:
            # Broadcast (empty list) when late-join is enabled so cached
            # command is accepted by any ECU; otherwise target only known ECUs.
            targets: List[str] = [] if self.allow_late_join else self.expected_ecus
        else:
            targets = target_ecus
        self._target_firmware = firmware_version
        self._command_timestamp = time.time()

        command = {
            "command": CMD_START_UPDATE,
            "firmware_version": firmware_version,
            "target_ecus": targets,
            "timestamp": self._command_timestamp,
        }
        write(self._control_writer, command)

        print(f"[DDS] ▶  START_UPDATE command broadcast via DDS multicast")
        print(f"[DDS]    Firmware: {firmware_version}")
        print(f"[DDS]    Targets : {', '.join(targets)}")
        print(f"[DDS]    QoS     : {self.qos.reliability.value} + "
              f"{self.qos.durability.value}\n")

    def wait_for_completion(self, timeout: float = 60.0) -> bool:
        """
        Block until all ECUs reach DONE or ERROR, or timeout expires.
        Returns True if all ECUs completed successfully.
        """
        completed = self._completion_event.wait(timeout=timeout)
        return completed

    def is_complete(self) -> bool:
        """Return True if all expected ECUs have reached a terminal state."""
        return self._completion_event.is_set()

    def get_ecu_states(self) -> dict:
        """Return a thread-safe snapshot of current ECU states."""
        with self._lock:
            return dict(self.ecu_states)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def get_metrics(self) -> dict:
        """
        Return collected metrics dict.

        Metrics captured:
          - per_ecu_latency_ms: time from command to DONE for each ECU
          - command_to_first_ack_ms: time until first ECU left IDLE
          - jitter_ms: max - min of per_ecu_latency_ms (sync spread)
          - end_to_end_ms: time from command to last ECU reaching DONE
          - error_count: number of ECUs that errored
          - state_timestamps: full per-ECU state transition timeline
        """
        with self._lock:
            states = dict(self.ecu_states)
            timestamps = {k: dict(v) for k, v in self._state_timestamps.items()}
            cmd_ts = self._command_timestamp

        per_ecu_latency: Dict[str, float] = {}
        error_count = 0
        first_ack_ts = None

        for ecu_id in self.expected_ecus:
            ts = timestamps.get(ecu_id, {})
            done_ts = ts.get(OTAState.DONE.value)
            dl_ts   = ts.get(OTAState.DOWNLOADING.value)

            if cmd_ts and done_ts:
                per_ecu_latency[ecu_id] = (done_ts - cmd_ts) * 1000.0

            # First ECU to acknowledge (leave IDLE) = first DOWNLOADING timestamp
            if cmd_ts and dl_ts:
                ack_latency = (dl_ts - cmd_ts) * 1000.0
                if first_ack_ts is None or ack_latency < first_ack_ts:
                    first_ack_ts = ack_latency

            update = states.get(ecu_id)
            if update and update.state == OTAState.ERROR.value:
                error_count += 1

        latencies = list(per_ecu_latency.values())
        end_to_end = max(latencies) if latencies else 0.0
        jitter     = (max(latencies) - min(latencies)) if len(latencies) > 1 else 0.0

        return {
            "per_ecu_latency_ms":       per_ecu_latency,
            "command_to_first_ack_ms":  first_ack_ts or 0.0,
            "jitter_ms":                jitter,
            "end_to_end_ms":            end_to_end,
            "error_count":              error_count,
            "qos_profile":              self.qos.name,
            "state_timestamps":         timestamps,
            "num_ecus":                 len(self.expected_ecus),
            "firmware_version":         self._target_firmware,
            "late_join_ecus":           list(self.late_join_ecus),
            "late_join_count":          len(self.late_join_ecus),
        }

    # ------------------------------------------------------------------
    # Real-time display
    # ------------------------------------------------------------------

    def print_status_table(self, clear_lines: int = 0) -> int:
        """
        Print a formatted table of current ECU states with ANSI color codes.
        Uses cursor-up escape to refresh in-place.
        Returns the number of lines printed (for next refresh).
        """
        if clear_lines > 0:
            # Move cursor up and erase to end of screen to cleanly overwrite
            print(f"\033[{clear_lines}A\033[J", end="")

        header = (
            f"\n{_BOLD}{'ECU ID':<12} {'State':<14} {'Prog':>5} "
            f"{'Latency':>10} {'Last Update':<20}{_RESET}"
        )
        separator = "─" * 65

        lines = [header, separator]

        with self._lock:
            states_copy = dict(self.ecu_states)
            timestamps  = {k: dict(v) for k, v in self._state_timestamps.items()}
            cmd_ts      = self._command_timestamp

        for ecu_id in self.expected_ecus:
            update = states_copy.get(ecu_id)
            if not update:
                state_str = f"{_CYAN}WAITING{_RESET}"
                prog_str  = "  -"
                lat_str   = "     -"
                ts_str    = "-"
            else:
                color     = _STATE_COLORS.get(update.state, _RESET)
                state_str = f"{color}{update.state:<14}{_RESET}"
                prog_str  = f"{update.progress_percent:>4}%"
                ts_str    = f"{update.timestamp:.3f}"

                if cmd_ts and update.state == OTAState.DONE.value:
                    ts_map  = timestamps.get(ecu_id, {})
                    done_ts = ts_map.get(OTAState.DONE.value)
                    lat_ms  = (done_ts - cmd_ts) * 1000.0 if done_ts else 0.0
                    lat_str = f"{lat_ms:>8.1f}ms"
                elif cmd_ts and update.state == OTAState.ERROR.value:
                    ts_map   = timestamps.get(ecu_id, {})
                    error_ts = ts_map.get(OTAState.ERROR.value)
                    lat_ms   = (error_ts - cmd_ts) * 1000.0 if error_ts else 0.0
                    lat_str  = f"{lat_ms:>8.1f}ms"
                elif cmd_ts and update.state != OTAState.IDLE.value:
                    elapsed = (time.time() - cmd_ts) * 1000.0
                    lat_str = f"{elapsed:>8.1f}ms"
                else:
                    lat_str = "        -"

                if update.error_code:
                    state_str = f"{_RED}{update.state} ({update.error_code}){_RESET}"

            lines.append(
                f"{ecu_id:<12} {state_str} {prog_str} {lat_str} {ts_str}"
            )

        lines.append(separator)

        # ── Late-joining ECUs (auto-discovered via DDS) ───────────────────
        with self._lock:
            late_ecus = list(self.late_join_ecus)

        if late_ecus:
            lines.append(
                f"\n{_BOLD}  ★  Late-Joining ECUs "
                f"— auto-discovered via RTPS {_RESET}"
            )
            lines.append(separator)
            for ecu_id in late_ecus:
                update = states_copy.get(ecu_id)
                if not update:
                    state_str = f"{_CYAN}JOINING{_RESET}"
                    prog_str  = "  -"
                    lat_str   = "     -"
                    ts_str    = "-"
                else:
                    color     = _STATE_COLORS.get(update.state, _RESET)
                    state_str = f"{color}{update.state:<14}{_RESET}"
                    prog_str  = f"{update.progress_percent:>4}%"
                    ts_str    = f"{update.timestamp:.3f}"

                    if cmd_ts and update.state == OTAState.DONE.value:
                        ts_map  = timestamps.get(ecu_id, {})
                        done_ts = ts_map.get(OTAState.DONE.value)
                        lat_ms  = (done_ts - cmd_ts) * 1000.0 if done_ts else 0.0
                        lat_str = f"{lat_ms:>8.1f}ms"
                    elif cmd_ts and update.state == OTAState.ERROR.value:
                        ts_map   = timestamps.get(ecu_id, {})
                        error_ts = ts_map.get(OTAState.ERROR.value)
                        lat_ms   = (error_ts - cmd_ts) * 1000.0 if error_ts else 0.0
                        lat_str  = f"{lat_ms:>8.1f}ms"
                    elif cmd_ts and update.state != OTAState.IDLE.value:
                        elapsed = (time.time() - cmd_ts) * 1000.0
                        lat_str = f"{elapsed:>8.1f}ms"
                    else:
                        lat_str = "        -"

                    if update.error_code:
                        state_str = f"{_RED}{update.state} ({update.error_code}){_RESET}"

                lines.append(
                    f"{ecu_id:<12} {state_str} {prog_str} {lat_str} {ts_str}"
                    f"  {_YELLOW}★ LATE{_RESET}"
                )
            lines.append(separator)

        output = "\n".join(lines)
        print(output)
        # Count actual newlines (embedded \n in elements + join separators) plus
        # the one added by print(), so the next call moves the cursor up exactly
        # the right number of lines regardless of how many \n-prefixed elements exist.
        return output.count('\n') + 1

    def shutdown(self) -> None:
        """Graceful DDS participant cleanup."""
        shutdown(self._participant)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OTA Update Coordinator — DDS-based UCM Master"
    )
    parser.add_argument("--num-ecus", type=int, default=5,
                        help="Number of simulated ECUs")
    parser.add_argument("--firmware", default="2.0.0",
                        help="Target firmware version")
    parser.add_argument("--domain-id", type=int, default=0,
                        help="DDS domain ID")
    parser.add_argument("--qos", default="reliable",
                        choices=["reliable", "best_effort"],
                        help="QoS profile (reliable/best_effort)")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="Maximum wait time in seconds")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    qos = RELIABLE_QOS if args.qos == "reliable" else BEST_EFFORT_QOS

    ecu_ids = [f"ECU_{i:03d}" for i in range(1, args.num_ecus + 1)]

    print(f"\n{'='*65}")
    print(f"DDS — Automotive OTA Update Manager")
    print(f"{'='*65}")

    # Start ECUs
    ecus = []
    for ecu_id in ecu_ids:
        ecu = ECU(ecu_id, "1.0.0", args.domain_id, qos)
        ecu.start()
        ecus.append(ecu)

    # Small delay for DDS discovery
    time.sleep(0.3)

    manager = UpdateManager(ecu_ids, qos, args.domain_id)
    manager.start_update(args.firmware)

    # Real-time display loop
    lines_printed = 0
    while not manager.is_complete():
        lines_printed = manager.print_status_table(clear_lines=lines_printed)
        time.sleep(0.5)

    # Final table
    manager.print_status_table(clear_lines=lines_printed)

    # Print metrics
    metrics = manager.get_metrics()
    print(f"\n{_BOLD}{'='*65}")
    print("  FINAL METRICS")
    print(f"{'='*65}{_RESET}")
    print(f"  End-to-end latency : {_GREEN}{metrics['end_to_end_ms']:.1f} ms{_RESET}")
    print(f"  Jitter (sync spread): {_YELLOW}{metrics['jitter_ms']:.1f} ms{_RESET}")
    print(f"  First ACK latency   : {metrics['command_to_first_ack_ms']:.1f} ms")
    print(f"  Errors              : {_RED if metrics['error_count'] else _GREEN}"
          f"{metrics['error_count']}{_RESET}")

    # Cleanup
    manager.shutdown()
    for ecu in ecus:
        ecu.stop()
