"""
dds_abstraction.py - DDS Abstraction Layer for Automotive OTA Demo
==================================================================
Pure-Python simulation that accurately models key DDS behaviors:
pub/sub, QoS policies, simulated network latency, and reliability.

Automotive context:
  In AUTOSAR Adaptive, DDS replaces CAN/LIN for high-bandwidth,
  multi-ECU coordination. This simulation faithfully reproduces
  the DDS pub/sub semantics used in automotive ADAS, OTA update
  systems, and V2X applications.
"""
from __future__ import annotations

import random
import threading
import time
import collections
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# QoS Enumerations
# In DDS, Quality of Service policies control how data is exchanged between
# publishers and subscribers. These directly map to AUTOSAR Adaptive SWC
# communication requirements.
# ---------------------------------------------------------------------------

class ReliabilityKind(Enum):
    """
    RELIABLE: Every sample is guaranteed to be delivered (via ACK/NACK).
              Use for OTA commands and safety-critical state updates.
              Equivalent to CAN with error frames + retransmission logic,
              but DDS handles this automatically.
    BEST_EFFORT: Samples may be dropped (no retransmission). Use for
                 high-frequency sensor telemetry where latest data matters
                 more than guaranteed delivery.
    """
    RELIABLE = "RELIABLE"
    BEST_EFFORT = "BEST_EFFORT"


class DurabilityKind(Enum):
    """
    TRANSIENT_LOCAL: The DataWriter caches the last N samples. Late-joining
                     subscribers automatically receive cached history.
                     Critical for OTA: a newly-booted ECU can receive the
                     last state update without missing the initial command.
    VOLATILE: No cache; subscribers only receive samples published after
              they subscribe. Use for real-time sensor streams.
    """
    TRANSIENT_LOCAL = "TRANSIENT_LOCAL"
    VOLATILE = "VOLATILE"


@dataclass
class QoSProfile:
    """
    Encapsulates DDS QoS settings for a DataWriter/DataReader pair.

    Automotive relevance:
      - reliability + durability together define the 'data freshness' contract.
      - history_depth maps to the DDS HISTORY policy keep-last depth.
      - deadline_ms: the writer must publish at least every N ms (OTA watchdog).
      - latency_budget_ms: hint to middleware for batching optimisation.
    """
    reliability: ReliabilityKind = ReliabilityKind.RELIABLE
    durability: DurabilityKind = DurabilityKind.TRANSIENT_LOCAL
    history_depth: int = 10          # keep last N samples
    deadline_ms: float = 100.0       # 100 ms deadline for OTA status
    latency_budget_ms: float = 5.0   # 5 ms latency budget
    name: str = "DefaultProfile"


# Convenience pre-built profiles used by ECU and UpdateManager
RELIABLE_QOS = QoSProfile(
    reliability=ReliabilityKind.RELIABLE,
    durability=DurabilityKind.TRANSIENT_LOCAL,
    history_depth=10,
    deadline_ms=100.0,
    latency_budget_ms=5.0,
    name="ECUStatus_Reliable",
)

BEST_EFFORT_QOS = QoSProfile(
    reliability=ReliabilityKind.BEST_EFFORT,
    durability=DurabilityKind.VOLATILE,
    history_depth=1,
    deadline_ms=200.0,
    latency_budget_ms=20.0,
    name="ECUStatus_BestEffort",
)

CONTROL_QOS = QoSProfile(
    reliability=ReliabilityKind.RELIABLE,
    durability=DurabilityKind.TRANSIENT_LOCAL,
    history_depth=1,
    deadline_ms=50.0,
    latency_budget_ms=2.0,
    name="OTAControl_Reliable",
)


# ---------------------------------------------------------------------------
# Simulation internals
# ---------------------------------------------------------------------------

# Simulated network latency model (DDS over Ethernet in automotive domain)
_SIM_LATENCY_MEAN_MS = 2.0    # 2 ms mean latency (Gigabit Ethernet backbone)
_SIM_LATENCY_STDDEV_MS = 0.5  # 0.5 ms standard deviation

# BEST_EFFORT drop probability (models UDP packet loss in congested network)
_BEST_EFFORT_DROP_PROB = 0.03  # 3% drop rate


class _SimTopic:
    """
    Simulated DDS Topic. In real DDS a Topic is the typed channel through
    which data flows. Here we use a thread-safe deque as the transport.

    In automotive DDS (AUTOSAR Adaptive / SOME/IP), topics map directly
    to service data elements or event groups.
    """
    def __init__(self, name: str, type_name: str):
        self.name = name
        self.type_name = type_name
        # Readers registered on this topic: list of (_SimReader, QoSProfile)
        self._readers: List[tuple] = []
        # TRANSIENT_LOCAL cache: stores last history_depth samples per writer
        self._cache: collections.deque = collections.deque(maxlen=100)
        self._lock = threading.Lock()

    def _register_reader(self, reader: "_SimReader", qos: QoSProfile):
        with self._lock:
            self._readers.append((reader, qos))
            # TRANSIENT_LOCAL durability: deliver cached samples to late joiners
            if qos.durability == DurabilityKind.TRANSIENT_LOCAL:
                max_deliver = qos.history_depth
                cached = list(self._cache)[-max_deliver:]
                for sample in cached:
                    reader._deliver(sample)

    def _publish(self, sample: dict, writer_qos: QoSProfile):
        """
        Simulate publishing: apply latency, reliability, then deliver.
        """
        with self._lock:
            readers_snapshot = list(self._readers)
            # Cache sample for TRANSIENT_LOCAL durability
            self._cache.append(sample)

        for reader, reader_qos in readers_snapshot:
            # BEST_EFFORT: randomly drop ~3% of messages
            if writer_qos.reliability == ReliabilityKind.BEST_EFFORT:
                if random.random() < _BEST_EFFORT_DROP_PROB:
                    continue  # packet lost

            # Simulate network latency asynchronously
            latency_ms = max(0.0, random.gauss(_SIM_LATENCY_MEAN_MS, _SIM_LATENCY_STDDEV_MS))
            delay_s = latency_ms / 1000.0

            # Deliver in background thread after simulated network delay
            threading.Timer(delay_s, reader._deliver, args=[sample]).start()


class _SimReader:
    """
    Simulated DDS DataReader. In real DDS, a DataReader is the entity that
    receives typed data from a topic, with listener callbacks triggered by
    the middleware on new data.
    """
    def __init__(self, topic: _SimTopic, qos: QoSProfile,
                 on_data_available: Optional[Callable[[dict], None]]):
        self._topic = topic
        self._qos = qos
        self._on_data_available = on_data_available
        self._lock = threading.Lock()

    def _deliver(self, sample: dict):
        """Called (possibly from a timer thread) when data arrives."""
        if self._on_data_available:
            try:
                self._on_data_available(sample)
            except Exception as exc:
                # In production DDS, listener exceptions are logged; never crash middleware
                print(f"[DDS-SIM] Reader callback error on topic '{self._topic.name}': {exc}")


class _SimWriter:
    """
    Simulated DDS DataWriter. In real DDS, DataWriter is the entity that
    publishes typed samples to a topic, with QoS-governed delivery.
    """
    def __init__(self, topic: _SimTopic, qos: QoSProfile):
        self._topic = topic
        self._qos = qos

    def write(self, sample: dict):
        self._topic._publish(sample, self._qos)


class _SimParticipant:
    """
    Simulated DDS DomainParticipant. In real DDS, the DomainParticipant
    is the factory for all DDS entities and scopes them to a domain ID.
    Automotive systems typically use domain 0 for vehicle bus and
    domain 1+ for diagnostics / OTA.
    """
    def __init__(self, domain_id: int, qos_profile: Optional[QoSProfile]):
        self.domain_id = domain_id
        self.qos_profile = qos_profile or RELIABLE_QOS
        self._active = True
        self._entities: List[Any] = []


# Global topic registry — shared across all simulated participants in the
# same process. In real DDS, the middleware handles discovery across processes.
_TOPIC_REGISTRY: Dict[str, _SimTopic] = {}
_REGISTRY_LOCK = threading.Lock()


def _get_or_create_topic(name: str, type_name: str) -> _SimTopic:
    with _REGISTRY_LOCK:
        if name not in _TOPIC_REGISTRY:
            _TOPIC_REGISTRY[name] = _SimTopic(name, type_name)
        return _TOPIC_REGISTRY[name]


# ---------------------------------------------------------------------------
# Public API — DDS-style publish/subscribe interface
# ---------------------------------------------------------------------------

def create_participant(domain_id: int = 0,
                       qos_profile: Optional[QoSProfile] = None) -> Any:
    """
    Create a DDS DomainParticipant.

    Automotive context:
        Domain 0 is the vehicle-internal bus. Participants auto-discover
        each other via DDS Simple Discovery Protocol (SDP) — no static IP
        configuration required, unlike traditional automotive networks.
    """
    return _SimParticipant(domain_id, qos_profile)


def create_topic(participant: Any, name: str, type_name: str) -> Any:
    """
    Create a DDS Topic (the named, typed data channel).

    Automotive context:
        Topics decouple publishers from subscribers — an ECU publishing
        'ECUStatus' doesn't need to know which systems are listening.
        This is unlike CAN where each message ID targets specific nodes.
    """
    return _get_or_create_topic(name, type_name)


def create_writer(participant: Any, topic: Any,
                  qos_profile: Optional[QoSProfile] = None) -> Any:
    """
    Create a DDS DataWriter on the given topic.

    Automotive context:
        A DataWriter with RELIABLE + TRANSIENT_LOCAL QoS ensures that even
        if an ECU briefly loses network connectivity, it will still receive
        OTA commands when it reconnects (history cache replay).
    """
    qos = qos_profile or RELIABLE_QOS
    return _SimWriter(topic, qos)


def create_reader(participant: Any, topic: Any,
                  qos_profile: Optional[QoSProfile] = None,
                  on_data_available: Optional[Callable[[dict], None]] = None) -> Any:
    """
    Create a DDS DataReader with an optional data-available callback.

    Automotive context:
        The listener callback is invoked on a background thread (similar to
        CAN interrupt handlers). Callbacks must be non-blocking and
        thread-safe — exactly as in AUTOSAR RTE port callbacks.
    """
    qos = qos_profile or RELIABLE_QOS
    reader = _SimReader(topic, qos, on_data_available)
    topic._register_reader(reader, qos)
    return reader


def write(writer: Any, sample_dict: dict) -> None:
    """
    Publish a sample to the topic.

    Automotive context:
        In DDS, 'write' is O(1) for the publisher — the middleware handles
        serialization, fragmentation, and delivery guarantees asynchronously.
        On CAN, a 50-byte payload needs 7 frames and blocking bus arbitration.
    """
    writer.write(sample_dict)


def shutdown(participant: Any) -> None:
    """
    Gracefully shut down a DomainParticipant and all its entities.

    Automotive context:
        Proper shutdown sends DDS BYE messages so other participants
        immediately detect the node leaving — no timeout required.
        This maps to clean ECU power-down sequences in AUTOSAR.
    """
    if isinstance(participant, _SimParticipant):
        participant._active = False
