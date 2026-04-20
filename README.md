# Automotive DDS OTA Update Coordination Demo

Multi-ECU Over-the-Air (OTA) firmware update coordination demo.
> Shows why DDS beats CAN/LIN for scalable, real-time automotive software-defined vehicle (SDV) communication.

---

## Overview

This demo simulates a fleet of automotive ECUs (Electronic Control Units) undergoing a
coordinated OTA firmware update orchestrated via DDS (Data Distribution Service) pub/sub middleware.

It demonstrates how and the AUTOSAR Adaptive DDS-based communication stack solves the scalability, reliability, and latency limitations of traditional CAN/LIN bus architectures
in modern Software-Defined Vehicles.

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DDS Domain 0                              │
│                                                                   │
│  ┌──────────────────┐    OTAControl (RELIABLE)   ┌────────────┐ │
│  │  UpdateManager   │──────────────────────────► │  ECU_001  │ │
│  │  (UCM Master)    │  Multicast → all ECUs       │  ECU_002  │ │
│  │                  │◄─────────────────────────── │  ECU_003  │ │
│  │  DDS Subscriber  │    ECUStatus (RELIABLE +    │  ECU_004  │ │
│  │  MetricsCollector│    TRANSIENT_LOCAL)         │  ECU_005  │ │
│  └──────────────────┘                             └────────────┘ │
│                                                                   │
│  Discovery: RTPS Simple Discovery Protocol (automatic, no config)│
│  Transport: UDP Multicast over Gigabit Ethernet                  │
│  vs CAN:    requires static message IDs, unicast, 1 Mbps max     │
└─────────────────────────────────────────────────────────────────┘
```

### Key Demo Points

| Feature | CAN 2.0B | DDS (simulated) |
|---|---|---|
| Command delivery | Unicast × N ECUs | Single multicast |
| 5-ECU OTA latency | ~25ms | ~4.5ms |
| 20-ECU bus load | ~60% (congested) | <0.01% |
| Late-joiner state sync | Manual polling | TRANSIENT_LOCAL (automatic) |
| Node discovery | Static IDs | RTPS SDP (automatic) |
| Reliability | Error frames + manual retry | RELIABLE QoS (automatic ACK/NACK) |
| Safety watchdog | Timeout-based (~500ms) | DDS Deadline + Liveliness (<100ms) |

---

## Prerequisites

- **Linux** (Ubuntu 20.04+ recommended) or macOS
- **Python 3.8+**
- No external DDS middleware required — all DDS behaviors are simulated in pure Python

---

## Installation

```bash
# Clone the repo
git clone https://github.com/eduardfz07/automotive-dds-demo.git
cd automotive-dds-demo

# Run the setup script (creates venv, installs deps, generates sample data)
chmod +x setup.sh && ./setup.sh

# Or manually:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python generate_sample_data.py
```

---

## Quick Start

```bash
source .venv/bin/activate

# Full demo: 5 ECUs, firmware 2.0.0, with CAN vs DDS comparison
python run_demo.py --num-ecus 5 --show-comparison

# Scale to 10 ECUs
python run_demo.py --num-ecus 10 --firmware 3.0.0

# Demonstrate QoS impact (BEST_EFFORT shows message loss)
python run_demo.py --num-ecus 5 --qos best_effort

# Inject error for a specific ECU after n amount of time
python run_demo.py --num-ecus 5 --inject-failure ECU_003 --failure-at 3.0

# Generate visualization charts
python visualize_results.py --generate-sample
```
---

## Module Descriptions

### `dds_abstraction.py`
Pure-Python DDS simulation layer. Accurately models pub/sub delivery, QoS policies
(RELIABLE/BEST_EFFORT, TRANSIENT_LOCAL/VOLATILE), simulated network latency
(Gaussian, 2ms mean / 0.5ms stddev), and BEST_EFFORT packet drop (~3%).

**Exports:** `ReliabilityKind`, `DurabilityKind`, `QoSProfile`,
`RELIABLE_QOS`, `BEST_EFFORT_QOS`, `CONTROL_QOS`, and the API functions:
`create_participant`, `create_topic`, `create_writer`, `create_reader`, `write`, `shutdown`.

### `ecu.py`
Simulated automotive ECU with a 7-state OTA state machine:
`IDLE → DOWNLOADING → VERIFYING → INSTALLING → REBOOTING → DONE` (or `ERROR`).

Each ECU is a DDS participant: publishes `ECUStateUpdate` on `ECUStatus` topic
(RELIABLE + TRANSIENT_LOCAL) and subscribes to commands on `OTAControl`.
5% chance of installation failure for resilience demo.

### `update_manager.py`
OTA Update Coordinator (UCM Master). Broadcasts `START_UPDATE` via DDS multicast,
monitors all ECU states, tracks per-ECU latency, and displays a real-time
ANSI-colored status table.

### `metrics_collector.py`
Metrics collection and analysis engine.

- `MetricsCollector`: records state transitions, calculates latency/jitter/throughput/E2E.
- `CANBusAnalyzer`: models CAN 2.0B overhead (frame count, bus load, congestion) vs DDS.
- `generate_scalability_data()`: produces DataFrame for visualization.
- `save_run_results()`: exports to `data/run_TIMESTAMP.csv`.

### `run_demo.py`
Main demo orchestrator. Runs all components, displays real-time status,
prints metrics summary, and optionally runs CAN vs DDS comparison.

### `visualize_results.py`
Generates 5 publication-quality charts (dark theme):
1. `scalability_comparison.png` — DDS vs CAN latency/overhead scaling
2. `latency_distribution.png` — Per-ECU latency violin plots (RELIABLE vs BEST_EFFORT)
3. `throughput_comparison.png` — DDS vs CAN throughput stacked bar chart
4. `state_timeline.png` — Gantt-style OTA state timeline
5. `qos_impact.png` — RELIABLE vs BEST_EFFORT QoS comparison

### `generate_sample_data.py`
Generates realistic pre-recorded metrics CSV files for 5, 10, 20 ECU scenarios.
Used for visualization without requiring a full demo run (suitable for screen recording).

---

## Metrics Explained

### Latency
Time from `START_UPDATE` command broadcast to ECU reaching `DONE` state.
- **DDS (5 ECUs):** ~4-6 seconds (OTA cycle) + ~2-4ms network delivery
- **CAN (5 ECUs):** ~4-6 seconds + ~25ms for 7 sequential CAN frames per state update

The network delivery latency difference is significant for 20+ ECUs:
CAN requires N unicast transmissions (O(N)); DDS uses one multicast (O(1)).

### Jitter
`max_latency - min_latency` across all ECUs. Measures fleet synchronization spread.
- **DDS:** typically 2-5ms (multicast delivery ± processing jitter)
- **CAN:** typically 15-40ms (serial bus arbitration + queuing delays)

Lower jitter = more synchronized ECU updates = better for A/B partition activation.

### Throughput
State-change DDS samples per second. 5 ECUs × 7 states × 10 progress updates ≈ 85 messages/OTA cycle.

### Scalability
CAN bus load grows O(N²) with ECU count (more ECUs = more frames = more congestion = more latency).
DDS multicast is O(1) — one transmission reaches all ECUs regardless of count.

---

## QoS Policies — Automotive Context

### RELIABLE vs BEST_EFFORT

| Policy | Use Case | Behavior | AUTOSAR Mapping |
|---|---|---|---|
| `RELIABLE` | OTA commands, safety state | ACK/NACK retransmission, guaranteed delivery | `FireAndReliable` method call |
| `BEST_EFFORT` | Sensor telemetry, diagnostics | No retransmission, ~3% drop OK | `FireAndForget` event group |

**Rule:** Always use `RELIABLE` for OTA commands and ECU state updates (ISO 26262 ASIL-B+).

### TRANSIENT_LOCAL vs VOLATILE

| Policy | Use Case | Behavior |
|---|---|---|
| `TRANSIENT_LOCAL` | OTA status, configuration | Writer caches last N samples; late joiners auto-receive history |
| `VOLATILE` | Real-time sensor streams | No cache; subscribers only get new data |

**Key demo point:** A newly-rebooted ECU with TRANSIENT_LOCAL can immediately receive the
current OTA command without waiting for a manual re-publish — impossible with CAN.

### Deadline QoS

Publisher MUST publish at least every N milliseconds. If violated, `on_requested_deadline_missed()`
callback fires on all subscribers — acting as a distributed safety watchdog.

- `ECUStatus_Reliable`: 100ms deadline → ECU health monitoring
- `OTAControl_Reliable`: 50ms deadline → OTA master liveness monitoring

### Liveliness QoS

DDS participants assert liveness periodically. If an ECU crashes, other participants
detect the liveliness loss within `lease_duration` (500ms in our config) — vs. CAN
which relies on message-level timeout detection (~1-3 seconds).

---

## CAN/LIN vs DDS Comparison

### CAN 2.0B Limitations for OTA

1. **8-byte data limit per frame** — a 50-byte OTA state update requires 7 CAN frames
2. **1 Mbps shared bus** — all ECUs contend for bandwidth; no true multicast
3. **Unicast messaging** — UpdateManager must send N messages to reach N ECUs (O(N))
4. **No built-in reliability** — error frames + application-level retry logic required
5. **Static IDs** — every new ECU type requires vehicle bus database update (DBC file)
6. **No late-joiner support** — ECU that missed a message must re-request it via polling
7. **Security** — no built-in authentication; DDS supports Auth, Encryption (DDS Security)

### DDS/RTPS Advantages

1. **True multicast** — single `write()` reaches all N ECUs simultaneously
2. **GbE transport** — 1000 Mbps bandwidth; OTA traffic is <0.01% at 100 ECUs
3. **QoS-governed delivery** — RELIABLE/BEST_EFFORT, deadlines, latency budgets
4. **Auto-discovery** — no static configuration; ECUs find each other via RTPS SDP
5. **TRANSIENT_LOCAL durability** — late joiners get history automatically
6. **DDS Security** — encryption, authentication, access control (OMG DDS Security 1.1)
7. **Typed data** — IDL-generated types with CDR serialization (~30 bytes vs 50+ for CAN)

---

## AUTOSAR Adaptive Relevance

AUTOSAR Adaptive Platform (AP) mandates DDS (via `ara::com`) as the primary IPC
mechanism for high-bandwidth ECU-to-ECU communication:

- **ara::com DDS binding**: maps `ara::com` service interfaces to DDS topics/types
- **UCM (Update and Configuration Management)**: uses DDS for OTA coordination — exactly what this demo models
- **ExecutionManagement + StateManagement**: publish ECU lifecycle states on DDS topics
- **PHM (Platform Health Management)**: uses DDS Liveliness and Deadline for health monitoring

This demo's state machine (`IDLE → DOWNLOADING → VERIFYING → INSTALLING → REBOOTING → DONE`)
directly mirrors the AUTOSAR UCM `PackageManagement` state machine.

---

## Running Pre-recorded vs Live Demo

### Pre-recorded version

```bash
# Generate all sample data and charts
python generate_sample_data.py
python visualize_results.py --generate-sample

# Charts are in plots/
ls plots/
```

### Live Demo

```bash
# 5 ECUs — runs in ~15 seconds
python run_demo.py --num-ecus 5 --show-comparison

# 10 ECUs — runs in ~20 seconds
python run_demo.py --num-ecus 10 --firmware 2.1.0
```

---

## Extending the Demo

### More ECUs
```bash
python run_demo.py --num-ecus 20 --duration 60
```

### Custom QoS
Edit the QoS profile in your code:
```python
from dds_abstraction import QoSProfile, ReliabilityKind, DurabilityKind
custom_qos = QoSProfile(
    reliability=ReliabilityKind.RELIABLE,
    durability=DurabilityKind.TRANSIENT_LOCAL,
    history_depth=20,
    deadline_ms=50.0,
    latency_budget_ms=1.0,
    name="Custom_StrictRealTime",
)
```

### Adding ECU Error Injection
In `ecu.py`, adjust `_INSTALL_ERROR_PROB` (default 5%) to increase fault rates
and demonstrate DDS-based resilience detection.

### Multi-domain Simulation
```python
# Separate domains for vehicle bus vs diagnostics
ecu1 = ECU("ECU_001", domain_id=0)  # vehicle bus
ecu2 = ECU("ECU_002", domain_id=1)  # diagnostics domain
```

---

## Example Output

```
[DDS] ▶  START_UPDATE command broadcast via DDS multicast
[DDS]    Firmware: 2.0.0
[DDS]    Targets : ECU_001, ECU_002, ECU_003, ECU_004, ECU_005

ECU ID       State          Prog    Latency  Last Update
─────────────────────────────────────────────────────────────────
ECU_001      DONE           100%   13241.2ms 1704067234.123
ECU_002      DONE           100%   13891.5ms 1704067234.456
ECU_003      REBOOTING      100%   13156.1ms 1704067234.078
ECU_004      INSTALLING       0%   12845.3ms 1704067233.990
ECU_005      DONE           100%   14102.8ms 1704067234.789

─────────────────────────────────────────────────────────────────
  End-to-end latency : 14102.8 ms
  Jitter (sync spread):  861.6 ms
  First ACK latency   :    2.3 ms
  Errors              :  0
```

---

## File Structure

```
automotive-dds-demo/
├── dds_abstraction.py      # Pure-Python DDS simulation layer
├── ecu.py                  # Simulated ECU with OTA state machine
├── update_manager.py       # OTA Update Coordinator (UCM Master)
├── metrics_collector.py    # Metrics & CAN vs DDS analysis
├── run_demo.py             # Main demo orchestrator
├── visualize_results.py    # Matplotlib visualization suite
├── generate_sample_data.py # Pre-recorded sample data generator
├── requirements.txt        # Python dependencies
├── setup.sh                # Linux setup script
├── README.md               # This file
├── data/                   # Generated CSV metrics
└── plots/                  # Generated PNG charts
```

---

## License

MIT

---