"""
ecu.py - Simulated ECU with OTA State Machine
==============================================
Models an automotive Electronic Control Unit (ECU) undergoing an
over-the-air (OTA) firmware update coordinated via DDS.

AUTOSAR context:
  In AUTOSAR Adaptive, each ECU runs an Execution Management (EM) and
  Update and Configuration Management (UCM) component. The UCM exposes
  a standardized OTA state machine (kIdle → kDownloading → kVerifying →
  kInstalling → kActivating → kDone). This demo models the same states
  using DDS pub/sub instead of proprietary vehicle bus messages.

DDS role:
  Each ECU is a DDS participant. It publishes state updates on the
  'ECUStatus' topic and subscribes to OTA commands on 'OTAControl'.
  RELIABLE + TRANSIENT_LOCAL QoS ensures command delivery even if the
  ECU is temporarily offline (e.g., during a controlled power cycle).
"""
from __future__ import annotations

import argparse
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from dds_abstraction import (
    QoSProfile,
    RELIABLE_QOS,
    BEST_EFFORT_QOS,
    create_participant,
    create_topic,
    create_writer,
    create_reader,
    write,
    shutdown,
)


# ---------------------------------------------------------------------------
# OTA State Machine States
# Maps to AUTOSAR UCM::PackageManagement state machine
# ---------------------------------------------------------------------------

class OTAState(Enum):
    """
    OTA firmware update states.

    AUTOSAR UCM mapping:
      IDLE        → kIdle
      DOWNLOADING → kTransferring
      VERIFYING   → kTransferring (integrity check phase)
      INSTALLING  → kProcessing
      REBOOTING   → kActivating
      DONE        → kIdle (new firmware active)
      ERROR       → kRollingBack / kCleaningUp
    """
    IDLE        = "IDLE"
    DOWNLOADING = "DOWNLOADING"
    VERIFYING   = "VERIFYING"
    INSTALLING  = "INSTALLING"
    REBOOTING   = "REBOOTING"
    DONE        = "DONE"
    ERROR       = "ERROR"


# ---------------------------------------------------------------------------
# DDS Data Types
# ---------------------------------------------------------------------------

@dataclass
class ECUStateUpdate:
    """
    DDS sample published on 'ECUStatus' topic.

    The @key annotation on ecu_id means each ECU has its own instance
    in the DDS domain — a subscriber can filter by ECU ID efficiently.
    """
    ecu_id: str = ""
    state: str = OTAState.IDLE.value
    timestamp: float = 0.0
    firmware_version: str = "1.0.0"
    progress_percent: int = 0
    error_code: str = ""
    sequence_number: int = 0

    def to_dict(self) -> dict:
        return {
            "ecu_id": self.ecu_id,
            "state": self.state,
            "timestamp": self.timestamp,
            "firmware_version": self.firmware_version,
            "progress_percent": self.progress_percent,
            "error_code": self.error_code,
            "sequence_number": self.sequence_number,
        }

    @staticmethod
    def from_dict(d: dict) -> "ECUStateUpdate":
        return ECUStateUpdate(
            ecu_id=d.get("ecu_id", ""),
            state=d.get("state", OTAState.IDLE.value),
            timestamp=d.get("timestamp", 0.0),
            firmware_version=d.get("firmware_version", "1.0.0"),
            progress_percent=d.get("progress_percent", 0),
            error_code=d.get("error_code", ""),
            sequence_number=d.get("sequence_number", 0),
        )


# OTA command constants (published on OTAControl topic)
CMD_START_UPDATE  = "START_UPDATE"
CMD_ABORT_UPDATE  = "ABORT_UPDATE"
CMD_QUERY_STATUS  = "QUERY_STATUS"


# ---------------------------------------------------------------------------
# ECU Class
# ---------------------------------------------------------------------------

class ECU:
    """
    Simulated automotive ECU with OTA state machine.

    Each ECU instance:
      1. Creates a DDS DomainParticipant in the specified domain.
      2. Publishes ECUStateUpdate samples on 'ECUStatus' (RELIABLE + TRANSIENT_LOCAL).
      3. Subscribes to OTA commands on 'OTAControl' (RELIABLE + TRANSIENT_LOCAL).
      4. Runs an autonomous state machine in a background thread.

    Processing jitter:
      Real ECUs have variable processing times due to OS scheduling, flash
      write latency, and CPU load. We model this with gaussian jitter
      (mean 35ms, stddev 10ms) added to each state transition duration.
    """

    # Per-ECU processing jitter model (milliseconds)
    _JITTER_MEAN_MS   = 35.0
    _JITTER_STDDEV_MS = 10.0

    # Probability of installation failure (simulates CRC mismatch, flash error)
    _INSTALL_ERROR_PROB = 0.05  # 5%

    def __init__(
        self,
        ecu_id: str,
        firmware_version: str = "1.0.0",
        domain_id: int = 0,
        qos_profile: Optional[QoSProfile] = None,
    ):
        self.ecu_id = ecu_id
        self.firmware_version = firmware_version
        self.domain_id = domain_id
        self.qos = qos_profile or RELIABLE_QOS

        # State machine internals
        self._state = OTAState.IDLE
        self._target_firmware: Optional[str] = None
        self._progress = 0
        self._seq = 0
        self._error_code = ""
        self._lock = threading.Lock()

        # Command queue (thread-safe)
        self._command_queue: list = []
        self._cmd_lock = threading.Lock()

        # Background thread
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # DDS entities
        self._participant = create_participant(domain_id, self.qos)
        self._status_topic  = create_topic(self._participant, "ECUStatus",  "ECUStateUpdate")
        self._control_topic = create_topic(self._participant, "OTAControl", "OTACommand")

        # DataWriter: publish state updates with RELIABLE + TRANSIENT_LOCAL
        self._status_writer = create_writer(self._participant, self._status_topic, self.qos)

        # DataReader: subscribe to OTA commands
        self._control_reader = create_reader(
            self._participant,
            self._control_topic,
            RELIABLE_QOS,           # Always use RELIABLE for commands
            on_data_available=self._on_command_received,
        )

        # Publish initial IDLE state (TRANSIENT_LOCAL cache will hold this
        # so late-joining monitors see the initial state)
        self._publish_state()

    # ------------------------------------------------------------------
    # DDS Callback
    # ------------------------------------------------------------------

    def _on_command_received(self, sample: dict) -> None:
        """
        DDS listener callback — invoked by the middleware on a reader thread.

        In AUTOSAR Adaptive, this maps to a port callback in the RTE layer.
        IMPORTANT: Must be non-blocking; heavy work goes to the state machine thread.
        """
        target = sample.get("target_ecus", [])
        # Accept command if targeted at this ECU or broadcast (empty list)
        if not target or self.ecu_id in target:
            with self._cmd_lock:
                self._command_queue.append(sample)

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------

    def _publish_state(self) -> None:
        """Publish current state as a DDS sample."""
        with self._lock:
            self._seq += 1
            update = ECUStateUpdate(
                ecu_id=self.ecu_id,
                state=self._state.value,
                timestamp=time.time(),
                firmware_version=self._target_firmware or self.firmware_version,
                progress_percent=self._progress,
                error_code=self._error_code,
                sequence_number=self._seq,
            )
        write(self._status_writer, update.to_dict())

    # ------------------------------------------------------------------
    # State machine helpers
    # ------------------------------------------------------------------

    def _jitter_sleep(self, base_s: float) -> None:
        """Sleep for base_s plus gaussian jitter, then publish progress."""
        jitter_s = random.gauss(self._JITTER_MEAN_MS, self._JITTER_STDDEV_MS) / 1000.0
        time.sleep(max(0.0, base_s + jitter_s))

    def _transition_to(self, new_state: OTAState, progress: int = 0,
                       error_code: str = "") -> None:
        """Thread-safe state transition with DDS publication."""
        with self._lock:
            old = self._state
            self._state = new_state
            self._progress = progress
            self._error_code = error_code
        self._publish_state()

    # ------------------------------------------------------------------
    # State machine — runs in background thread
    # ------------------------------------------------------------------

    def _run_state_machine(self) -> None:
        """
        Main state machine loop.

        Each state has realistic timing modeled after real ECU flash update
        benchmarks on ARM Cortex-R5 (typical AUTOSAR ECU):
          - Downloading: ~2-4s for a 10MB firmware image at simulated 5 MB/s
          - Verifying:   ~1-2s for SHA-256 hash verification
          - Installing:  ~1-2s for flash write + verify
          - Rebooting:   ~0.5-1s for controlled power cycle
        """
        while self._running:
            # Drain pending commands
            cmd = None
            with self._cmd_lock:
                if self._command_queue:
                    cmd = self._command_queue.pop(0)

            if cmd:
                self._handle_command(cmd)

            with self._lock:
                current = self._state

            # Autonomous state progressions (no command needed after start)
            if current == OTAState.DOWNLOADING:
                self._do_downloading()
            elif current == OTAState.VERIFYING:
                self._do_verifying()
            elif current == OTAState.INSTALLING:
                self._do_installing()
            elif current == OTAState.REBOOTING:
                self._do_rebooting()
            else:
                # IDLE / DONE / ERROR — just wait for commands
                time.sleep(0.05)

    def _handle_command(self, cmd: dict) -> None:
        """Process an OTA command received from the UpdateManager."""
        command = cmd.get("command", "")
        with self._lock:
            current = self._state

        if command == CMD_START_UPDATE and current == OTAState.IDLE:
            self._target_firmware = cmd.get("firmware_version", "2.0.0")
            self._transition_to(OTAState.DOWNLOADING, progress=0)

        elif command == CMD_ABORT_UPDATE and current not in (
            OTAState.DONE, OTAState.REBOOTING, OTAState.ERROR
        ):
            self._transition_to(OTAState.ERROR, error_code="ABORTED")

        elif command == CMD_QUERY_STATUS:
            # Republish current state (no transition)
            self._publish_state()

    def _do_downloading(self) -> None:
        """
        Simulate firmware download with progress updates.
        Publishes every 10% increment — demonstrates DDS throughput.
        At 5 ECUs × 10 updates = 50 DDS samples just for download progress.
        On CAN: 50 × 7 frames = 350 CAN frames for the same information.
        """
        total_duration = random.uniform(2.0, 4.0)  # 2–4 seconds
        steps = 10
        step_duration = total_duration / steps

        for i in range(1, steps + 1):
            if not self._running:
                return
            self._jitter_sleep(step_duration)
            with self._lock:
                if self._state != OTAState.DOWNLOADING:
                    return  # aborted
            self._transition_to(OTAState.DOWNLOADING, progress=i * 10)

        self._transition_to(OTAState.VERIFYING, progress=0)

    def _do_verifying(self) -> None:
        """
        Simulate firmware integrity verification (SHA-256 / RSA signature check).
        In AUTOSAR SecOC, this maps to the Message Authentication Code
        verification step before any update is applied.
        """
        self._jitter_sleep(random.uniform(1.0, 2.0))
        with self._lock:
            if self._state != OTAState.VERIFYING:
                return
        self._transition_to(OTAState.INSTALLING, progress=0)

    def _do_installing(self) -> None:
        """
        Simulate flash write + A/B partition swap.
        5% chance of installation failure — demonstrates DDS-based
        resilience coordination (UpdateManager can detect and react in <100ms,
        vs. CAN timeout-based detection which takes ~500ms).
        """
        self._jitter_sleep(random.uniform(1.0, 2.0))
        with self._lock:
            if self._state != OTAState.INSTALLING:
                return

        # Simulate random installation failure
        if random.random() < self._INSTALL_ERROR_PROB:
            self._transition_to(OTAState.ERROR, error_code="INSTALL_FAILED_CRC")
            return

        self._transition_to(OTAState.REBOOTING, progress=100)

    def _do_rebooting(self) -> None:
        """
        Simulate controlled ECU reboot / partition activation.
        In a real system, the ECU sends a final DDS BYE before power-cycling.
        TRANSIENT_LOCAL durability ensures the DONE state is still visible
        to monitoring systems after the reboot.
        """
        self._jitter_sleep(random.uniform(0.5, 1.0))
        with self._lock:
            if self._state != OTAState.REBOOTING:
                return
        # Update the firmware version to the new target
        with self._lock:
            if self._target_firmware:
                self.firmware_version = self._target_firmware
        self._transition_to(OTAState.DONE, progress=100)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the ECU state machine in a background thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run_state_machine,
            name=f"ECU-{self.ecu_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Graceful shutdown — signals the state machine thread to exit."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        shutdown(self._participant)

    @property
    def state(self) -> OTAState:
        with self._lock:
            return self._state

    @property
    def progress(self) -> int:
        with self._lock:
            return self._progress


# ---------------------------------------------------------------------------
# CLI entry point — run a single ECU standalone for testing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulated automotive ECU with OTA state machine"
    )
    parser.add_argument("--ecu-id",    default="ECU_001",  help="ECU identifier")
    parser.add_argument("--firmware",  default="1.0.0",    help="Current firmware version")
    parser.add_argument("--num-ecus",  type=int, default=5, help="(Ignored in standalone mode)")
    parser.add_argument("--domain-id", type=int, default=0, help="DDS domain ID")
    parser.add_argument("--qos",       default="reliable",
                        choices=["reliable", "best_effort"], help="QoS profile")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    qos = RELIABLE_QOS if args.qos == "reliable" else BEST_EFFORT_QOS

    print(f"\n{'='*60}")
    print(f"  Automotive ECU Simulator — {args.ecu_id}")
    print(f"  Firmware: {args.firmware}  |  Domain: {args.domain_id}")
    print(f"  QoS: {qos.name}")
    print(f"{'='*60}\n")

    ecu = ECU(args.ecu_id, args.firmware, args.domain_id, qos)
    ecu.start()

    print(f"[{args.ecu_id}] ECU started. Waiting for OTA command on 'OTAControl' topic.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1.0)
            print(f"[{args.ecu_id}] State: {ecu.state.value}  Progress: {ecu.progress}%")
    except KeyboardInterrupt:
        print(f"\n[{args.ecu_id}] Shutting down...")
        ecu.stop()
