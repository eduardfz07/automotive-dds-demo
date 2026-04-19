"""
visualize_results.py - Matplotlib Visualization Suite
======================================================
Generates 5 publication-quality charts for the RTI interview demo.
All charts use a professional dark theme inspired by RTI branding.

Charts:
  1. scalability_comparison.png  — DDS vs CAN latency/overhead scaling
  2. latency_distribution.png    — Per-ECU latency violin plots
  3. throughput_comparison.png   — DDS vs CAN throughput bar chart
  4. state_timeline.png          — Gantt-style OTA state timeline
  5. qos_impact.png              — RELIABLE vs BEST_EFFORT comparison

Usage:
  python visualize_results.py --generate-sample --show
  python visualize_results.py --data-dir data/ --output-dir plots/
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

try:
    import matplotlib
    matplotlib.use("Agg")  # non-interactive backend by default
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.patches import FancyArrowPatch
    import numpy as np
    import pandas as pd
    _PLOT_AVAILABLE = True
except ImportError:
    _PLOT_AVAILABLE = False
    print("[VIZ] matplotlib/numpy/pandas not installed. Run: pip install matplotlib numpy pandas")
    sys.exit(1)


# ---------------------------------------------------------------------------
# RTI-inspired professional dark theme
# ---------------------------------------------------------------------------

RTI_DARK_BG    = "#1a1a2e"
RTI_PANEL_BG   = "#16213e"
RTI_BLUE       = "#0f3460"
RTI_ACCENT     = "#e94560"
RTI_GREEN      = "#4ecca3"
RTI_YELLOW     = "#f5a623"
RTI_GRAY       = "#8892b0"
RTI_WHITE      = "#ccd6f6"
RTI_TEXT       = "#e6f1ff"

_PALETTE_DDS = [RTI_GREEN, "#57b8ff", "#b0f4e6", "#7ee8a2"]
_PALETTE_CAN = [RTI_ACCENT, "#ff8080", "#ff6b6b", "#ffa07a"]

_STATE_COLORS = {
    "IDLE":        RTI_GRAY,
    "DOWNLOADING": RTI_YELLOW,
    "VERIFYING":   "#57b8ff",
    "INSTALLING":  "#b57bee",
    "REBOOTING":   "#f5c842",
    "DONE":        RTI_GREEN,
    "ERROR":       RTI_ACCENT,
}

def _apply_dark_theme(fig, ax_list) -> None:
    """Apply consistent dark theme to figure and axes."""
    fig.patch.set_facecolor(RTI_DARK_BG)
    for ax in (ax_list if isinstance(ax_list, list) else [ax_list]):
        ax.set_facecolor(RTI_PANEL_BG)
        ax.tick_params(colors=RTI_TEXT, labelsize=9)
        ax.xaxis.label.set_color(RTI_TEXT)
        ax.yaxis.label.set_color(RTI_TEXT)
        ax.title.set_color(RTI_TEXT)
        for spine in ax.spines.values():
            spine.set_edgecolor(RTI_GRAY)
        ax.grid(True, color=RTI_BLUE, alpha=0.4, linestyle="--", linewidth=0.7)


def _save_fig(fig, output_dir: str, filename: str, show: bool) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    fig.savefig(path, dpi=150, bbox_inches="tight",
                facecolor=RTI_DARK_BG, edgecolor="none")
    print(f"  ✓  Saved: {path}")
    if show:
        matplotlib.use("TkAgg")  # switch to interactive for display
        plt.show()
    plt.close(fig)


# ---------------------------------------------------------------------------
# 1. Scalability Comparison
# ---------------------------------------------------------------------------

def plot_scalability(data_dir: str, output_dir: str, show: bool) -> None:
    """
    Dual-axis line chart: latency (left) and frame count (right).
    CAN grows exponentially; DDS grows slowly. Shade 'CAN fails' region.
    """
    csv_path = os.path.join(data_dir, "scalability_comparison.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        # Generate inline if file missing
        from metrics_collector import CANBusAnalyzer
        rows = []
        for n in [5, 10, 20, 50, 100]:
            can = CANBusAnalyzer.calculate_can_overhead(n)
            dds = CANBusAnalyzer.calculate_dds_overhead(n)
            rows.append({
                "num_ecus":       n,
                "dds_latency_ms": dds["estimated_latency_ms"],
                "can_latency_ms": can["estimated_latency_ms"],
                "dds_frames":     dds["samples_total"],
                "can_frames":     can["frames_total"],
                "can_bus_load_pct": can["bus_load_pct"],
            })
        df = pd.DataFrame(rows)

    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax2 = ax1.twinx()
    _apply_dark_theme(fig, [ax1, ax2])
    ax2.set_facecolor("none")  # transparent so ax1 background shows

    x = df["num_ecus"].values

    # Latency lines
    ax1.plot(x, df["dds_latency_ms"], "o-", color=RTI_GREEN,  linewidth=2.5,
             markersize=7, label="DDS Latency (ms)", zorder=5)
    ax1.plot(x, df["can_latency_ms"], "s--", color=RTI_ACCENT, linewidth=2.5,
             markersize=7, label="CAN Latency (ms)", zorder=5)

    # Frame/message counts (secondary axis)
    ax2.bar(x - 1, df["dds_frames"], width=2.5, alpha=0.35,
            color=RTI_GREEN,  label="DDS Samples")
    ax2.bar(x + 1, df["can_frames"], width=2.5, alpha=0.35,
            color=RTI_ACCENT, label="CAN Frames")

    # Shade "CAN failure zone" — >50ms latency budget for OTA state updates
    can_fail_threshold = 50.0
    ax1.axhline(can_fail_threshold, color=RTI_YELLOW, linestyle=":",
                linewidth=1.5, alpha=0.8, label="50ms OTA Latency Budget")
    ax1.fill_between(x, can_fail_threshold, df["can_latency_ms"],
                     where=(df["can_latency_ms"] > can_fail_threshold),
                     alpha=0.15, color=RTI_ACCENT, label="CAN Exceeds Budget")

    ax1.set_xlabel("Number of ECUs", fontsize=11)
    ax1.set_ylabel("End-to-End Latency (ms)", color=RTI_TEXT, fontsize=11)
    ax2.set_ylabel("Message / Frame Count", color=RTI_GRAY, fontsize=11)
    ax2.tick_params(colors=RTI_GRAY)

    # Combined legend
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="upper left", framealpha=0.3,
               facecolor=RTI_PANEL_BG, edgecolor=RTI_GRAY,
               labelcolor=RTI_TEXT, fontsize=9)

    ax1.set_title(
        "Scalability: RTI Connext DDS vs CAN 2.0B for OTA Coordination",
        fontsize=13, fontweight="bold", pad=15, color=RTI_TEXT,
    )
    ax1.set_xticks(x)

    _save_fig(fig, output_dir, "scalability_comparison.png", show)


# ---------------------------------------------------------------------------
# 2. Latency Distribution (violin + box)
# ---------------------------------------------------------------------------

def plot_latency_distribution(data_dir: str, output_dir: str, show: bool) -> None:
    """
    Violin plot showing per-ECU latency spread for RELIABLE vs BEST_EFFORT QoS.
    """
    csv_path = os.path.join(data_dir, "latency_distribution.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        # Generate synthetic data inline
        import random as rng
        rng.seed(0)
        rows = []
        for run in range(50):
            for i in range(1, 6):
                ecu_id = f"ECU_{i:03d}"
                rows.append({"ecu_id": ecu_id, "qos": "RELIABLE",
                              "latency_ms": max(5000, rng.gauss(10000 + 500*i, 300))})
                rows.append({"ecu_id": ecu_id, "qos": "BEST_EFFORT",
                              "latency_ms": max(4000, rng.gauss(9500 + 600*i, 800))})
        df = pd.DataFrame(rows)

    # Convert to seconds for readability
    df["latency_s"] = df["latency_ms"] / 1000.0

    ecu_ids = sorted(df["ecu_id"].unique())
    n_ecus  = len(ecu_ids)
    x_pos   = np.arange(n_ecus)
    width   = 0.35

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    _apply_dark_theme(fig, [ax1, ax2])

    for ax, qos_name, color, title in [
        (ax1, "RELIABLE",    RTI_GREEN,  "RELIABLE QoS"),
        (ax2, "BEST_EFFORT", RTI_ACCENT, "BEST_EFFORT QoS"),
    ]:
        subset = df[df["qos"] == qos_name]
        groups = [subset[subset["ecu_id"] == eid]["latency_s"].values for eid in ecu_ids]

        parts = ax.violinplot(groups, positions=x_pos, widths=0.6,
                              showmeans=True, showmedians=True)
        for pc in parts["bodies"]:
            pc.set_facecolor(color)
            pc.set_alpha(0.55)
            pc.set_edgecolor(RTI_WHITE)
        parts["cmeans"].set_color(RTI_YELLOW)
        parts["cmedians"].set_color(RTI_WHITE)
        for part_name in ["cbars", "cmins", "cmaxes"]:
            parts[part_name].set_color(color)
            parts[part_name].set_alpha(0.6)

        ax.set_xticks(x_pos)
        ax.set_xticklabels(ecu_ids, rotation=30, ha="right", fontsize=9)
        ax.set_xlabel("ECU", fontsize=10)
        ax.set_ylabel("Latency (s)", fontsize=10)
        ax.set_title(
            f"End-to-End OTA Latency — {title}",
            fontsize=11, fontweight="bold", color=RTI_TEXT,
        )

        # Annotate mean per ECU
        for xi, grp in zip(x_pos, groups):
            if len(grp) > 0:
                ax.annotate(
                    f"{np.mean(grp):.1f}s",
                    xy=(xi, np.mean(grp)),
                    xytext=(xi + 0.15, np.mean(grp)),
                    fontsize=7, color=RTI_YELLOW,
                )

    fig.suptitle(
        "End-to-End OTA State Sync Latency per ECU (50 Runs)",
        fontsize=13, fontweight="bold", color=RTI_TEXT, y=1.01,
    )
    _save_fig(fig, output_dir, "latency_distribution.png", show)


# ---------------------------------------------------------------------------
# 3. Throughput Comparison
# ---------------------------------------------------------------------------

def plot_throughput(data_dir: str, output_dir: str, show: bool) -> None:
    """
    Stacked bar chart: useful data bytes vs protocol overhead for DDS and CAN.
    """
    from metrics_collector import CANBusAnalyzer

    ecu_counts = [5, 10, 20, 50, 100]
    dds_useful, dds_overhead, can_useful, can_overhead = [], [], [], []

    for n in ecu_counts:
        can = CANBusAnalyzer.calculate_can_overhead(n)
        dds = CANBusAnalyzer.calculate_dds_overhead(n)

        # DDS: 30 useful bytes per sample, 62 bytes network overhead
        dds_useful_bytes   = dds["samples_total"] * 30
        dds_overhead_bytes = dds["samples_total"] * 62
        dds_useful.append(dds_useful_bytes / 1000)
        dds_overhead.append(dds_overhead_bytes / 1000)

        # CAN: 8 useful bytes per frame, 65/8=8 bytes overhead per frame
        can_useful_bytes   = can["frames_total"] * 8
        can_overhead_bytes = can["frames_total"] * 8  # ~50% overhead in CAN frame
        can_useful.append(can_useful_bytes / 1000)
        can_overhead.append(can_overhead_bytes / 1000)

    x = np.arange(len(ecu_counts))
    width = 0.3

    fig, ax = plt.subplots(figsize=(12, 6))
    _apply_dark_theme(fig, ax)

    bars_dds_u  = ax.bar(x - width/2, dds_useful,   width, label="DDS — Payload (KB)",
                          color=RTI_GREEN,  alpha=0.85)
    bars_dds_oh = ax.bar(x - width/2, dds_overhead, width, bottom=dds_useful,
                          label="DDS — Protocol Overhead (KB)",
                          color="#1a7a5e", alpha=0.6)
    bars_can_u  = ax.bar(x + width/2, can_useful,   width, label="CAN — Payload (KB)",
                          color=RTI_ACCENT, alpha=0.85)
    bars_can_oh = ax.bar(x + width/2, can_overhead, width, bottom=can_useful,
                          label="CAN — Protocol Overhead (KB)",
                          color="#7a1a1a", alpha=0.6)

    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in ecu_counts])
    ax.set_xlabel("Number of ECUs", fontsize=11)
    ax.set_ylabel("Data Volume (KB per OTA cycle)", fontsize=11)
    ax.set_title(
        "Message Throughput: DDS vs CAN Bus Load",
        fontsize=13, fontweight="bold", color=RTI_TEXT,
    )
    ax.legend(framealpha=0.3, facecolor=RTI_PANEL_BG, edgecolor=RTI_GRAY,
              labelcolor=RTI_TEXT, fontsize=9)

    # Annotate efficiency ratio
    for xi, du, cu in zip(x, dds_useful, can_useful):
        total_dds = dds_useful[x.tolist().index(xi)] + dds_overhead[x.tolist().index(xi)]
        total_can = can_useful[x.tolist().index(xi)] + can_overhead[x.tolist().index(xi)]
        if total_can > 0:
            ratio = total_can / max(total_dds, 0.001)
            ax.text(xi, max(total_dds, total_can) * 1.05,
                    f"CAN={ratio:.1f}×\nmore overhead",
                    ha="center", va="bottom", fontsize=7,
                    color=RTI_YELLOW)

    _save_fig(fig, output_dir, "throughput_comparison.png", show)


# ---------------------------------------------------------------------------
# 4. State Timeline (Gantt)
# ---------------------------------------------------------------------------

def plot_state_timeline(data_dir: str, output_dir: str, show: bool) -> None:
    """
    Gantt-style chart showing OTA state progression per ECU.
    Demonstrates near-simultaneous DDS multicast delivery.
    """
    csv_path = os.path.join(data_dir, "state_timeline.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
    else:
        import random as rng
        rng.seed(7)
        rows = []
        state_durs = {"IDLE": 0.002, "DOWNLOADING": 3.0, "VERIFYING": 1.5,
                      "INSTALLING": 1.5, "REBOOTING": 0.75, "DONE": 0.1}
        for i in range(1, 6):
            t = rng.gauss(0.002, 0.001)
            for state, dur in state_durs.items():
                end = t + dur + rng.gauss(0, dur * 0.1)
                rows.append({"ecu_id": f"ECU_{i:03d}", "state": state,
                              "start_s": t, "end_s": end})
                t = end
        df = pd.DataFrame(rows)

    ecu_ids = sorted(df["ecu_id"].unique())
    states_order = ["IDLE", "DOWNLOADING", "VERIFYING",
                    "INSTALLING", "REBOOTING", "DONE"]

    fig, ax = plt.subplots(figsize=(14, 6))
    _apply_dark_theme(fig, ax)

    y_ticks = []
    y_labels = []

    for yi, ecu_id in enumerate(ecu_ids):
        ecu_df = df[df["ecu_id"] == ecu_id]
        y_ticks.append(yi)
        y_labels.append(ecu_id)

        for _, row in ecu_df.iterrows():
            state    = row.get("state", "IDLE")
            start    = float(row.get("start_s", 0))
            end      = float(row.get("end_s", start + 0.1))
            duration = end - start
            color    = _STATE_COLORS.get(state, RTI_GRAY)

            ax.barh(yi, duration, left=start, height=0.6,
                    color=color, alpha=0.85, edgecolor=RTI_PANEL_BG,
                    linewidth=0.5)

            # Label state name inside bar if wide enough
            if duration > 0.2:
                ax.text(start + duration / 2, yi, state[:4],
                        ha="center", va="center",
                        fontsize=7, color="white", fontweight="bold")

    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels, fontsize=10, color=RTI_TEXT)
    ax.set_xlabel("Time (seconds)", fontsize=11)
    ax.set_title(
        "OTA State Timeline: All ECUs (5 ECUs, RELIABLE QoS)",
        fontsize=13, fontweight="bold", color=RTI_TEXT,
    )

    # Legend
    legend_patches = [
        mpatches.Patch(color=_STATE_COLORS[s], label=s, alpha=0.85)
        for s in states_order
    ]
    ax.legend(handles=legend_patches, loc="lower right",
              framealpha=0.3, facecolor=RTI_PANEL_BG,
              edgecolor=RTI_GRAY, labelcolor=RTI_TEXT, fontsize=9,
              ncol=3)

    # Annotate: "DDS multicast — all ECUs receive command simultaneously"
    t_first = df["start_s"].min()
    ax.axvline(t_first + 0.002, color=RTI_YELLOW, linestyle="--",
               linewidth=1.2, alpha=0.8)
    ax.text(t_first + 0.005, len(ecu_ids) - 0.1,
            "DDS multicast\n(all ECUs ±2ms)",
            color=RTI_YELLOW, fontsize=8, va="top")

    _save_fig(fig, output_dir, "state_timeline.png", show)


# ---------------------------------------------------------------------------
# 5. QoS Impact
# ---------------------------------------------------------------------------

def plot_qos_impact(data_dir: str, output_dir: str, show: bool) -> None:
    """
    Grouped bar chart: RELIABLE vs BEST_EFFORT QoS metrics comparison.
    Shows latency, jitter, and message loss %.
    """
    csv_path = os.path.join(data_dir, "qos_comparison.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        # Use 5 ECU scenario for simplicity
        df5 = df[df["num_ecus"] == 5]
        if df5.empty:
            df5 = df.head(2)
    else:
        df5 = pd.DataFrame([
            {"qos": "RELIABLE",   "end_to_end_ms": 12.5, "jitter_ms": 3.1,
             "message_loss_pct": 0.0},
            {"qos": "BEST_EFFORT","end_to_end_ms": 11.2, "jitter_ms": 15.3,
             "message_loss_pct": 3.2},
        ])

    metrics_labels = ["End-to-End Latency (ms)", "Jitter (ms)", "Message Loss (%)"]
    metrics_cols   = ["end_to_end_ms", "jitter_ms", "message_loss_pct"]

    rel_row = df5[df5["qos"] == "RELIABLE"].iloc[0] if not df5[df5["qos"] == "RELIABLE"].empty else None
    be_row  = df5[df5["qos"] == "BEST_EFFORT"].iloc[0] if not df5[df5["qos"] == "BEST_EFFORT"].empty else None

    rel_vals = [float(rel_row[c]) if rel_row is not None and c in rel_row.index else 0.0
                for c in metrics_cols]
    be_vals  = [float(be_row[c])  if be_row  is not None and c in be_row.index  else 0.0
                for c in metrics_cols]

    # Normalize to show relative difference clearly
    x = np.arange(len(metrics_labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 6))
    _apply_dark_theme(fig, ax)

    bars_rel = ax.bar(x - width/2, rel_vals, width, label="RELIABLE QoS",
                       color=RTI_GREEN,  alpha=0.85)
    bars_be  = ax.bar(x + width/2, be_vals,  width, label="BEST_EFFORT QoS",
                       color=RTI_ACCENT, alpha=0.85)

    # Value annotations
    for bar in bars_rel:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.1,
                f"{h:.1f}", ha="center", va="bottom",
                fontsize=9, color=RTI_GREEN)
    for bar in bars_be:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h + 0.1,
                f"{h:.1f}", ha="center", va="bottom",
                fontsize=9, color=RTI_ACCENT)

    ax.set_xticks(x)
    ax.set_xticklabels(metrics_labels, fontsize=10)
    ax.set_ylabel("Value", fontsize=11)
    ax.set_title(
        "QoS Policy Impact on OTA Coordination (5 ECUs)",
        fontsize=13, fontweight="bold", color=RTI_TEXT,
    )
    ax.legend(framealpha=0.3, facecolor=RTI_PANEL_BG, edgecolor=RTI_GRAY,
              labelcolor=RTI_TEXT, fontsize=10)

    # Insight annotation
    ax.text(0.98, 0.98,
            "BEST_EFFORT: lower latency but\nhigher jitter and message loss.\n"
            "RELIABLE: mandatory for ISO 26262.",
            transform=ax.transAxes, fontsize=8, color=RTI_GRAY,
            va="top", ha="right",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=RTI_BLUE,
                      edgecolor=RTI_GRAY, alpha=0.7))

    _save_fig(fig, output_dir, "qos_impact.png", show)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def generate_all_plots(data_dir: str = "data/", output_dir: str = "plots/",
                       show: bool = False) -> None:
    """Generate all 5 visualization charts."""
    print(f"\n{'='*60}")
    print("  Generating Visualization Charts")
    print(f"{'='*60}\n")

    plot_scalability(data_dir, output_dir, show)
    plot_latency_distribution(data_dir, output_dir, show)
    plot_throughput(data_dir, output_dir, show)
    plot_state_timeline(data_dir, output_dir, show)
    plot_qos_impact(data_dir, output_dir, show)

    print(f"\n{'='*60}")
    print(f"  All charts saved to: {output_dir}")
    print(f"{'='*60}\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate automotive DDS demo visualization charts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python visualize_results.py --generate-sample --show
  python visualize_results.py --data-dir data/ --output-dir plots/
        """,
    )
    parser.add_argument("--data-dir",       default="data/",
                        help="Directory containing CSV data files")
    parser.add_argument("--output-dir",     default="plots/",
                        help="Directory to save plot images")
    parser.add_argument("--show",           action="store_true",
                        help="Display charts interactively (requires display)")
    parser.add_argument("--generate-sample", action="store_true",
                        help="Generate sample data before plotting")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.generate_sample:
        print("[VIZ] Generating sample data...")
        import generate_sample_data  # noqa: F401 — runs __main__ block via import
        # Call functions directly
        from generate_sample_data import (
            generate_scenario,
            generate_scalability_comparison,
            generate_qos_comparison,
            generate_latency_distribution,
            generate_state_timeline,
        )
        generate_scenario(5)
        generate_scenario(10)
        generate_scalability_comparison()
        generate_qos_comparison()
        generate_latency_distribution()
        generate_state_timeline()
        print()

    generate_all_plots(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        show=args.show,
    )
