"""
Plot frame timestamp distribution for single-frame mode output.

Usage:
    uv run python scripts/plot_frame_timing.py <output_dir>
    uv run python scripts/plot_frame_timing.py "C:/Program Files (x86)/MotorDrive_MiceCam/output"
"""

import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import numpy as np


def load_frame_log(path: Path) -> dict:
    """Load frame_log.csv, return parsed data."""
    rows = []
    with open(path, "r") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    arrival_ns = np.array([float(r["arrival_qpc_ns"]) for r in rows], dtype=np.float64)
    mf_pts = np.array([float(r["mf_pts_100ns"]) for r in rows], dtype=np.float64)
    write_lat = np.array([float(r["write_latency_ms"]) for r in rows], dtype=np.float64)
    sizes = np.array([int(r["bytes"]) for r in rows], dtype=np.int64)
    drops = int(rows[-1]["drop_count"]) if rows else 0

    # QPC inter-arrival intervals
    qpc_deltas_ms = np.diff(arrival_ns) / 1e6

    # MF PTS intervals
    pts_deltas_ms = np.diff(mf_pts) / 1e4  # 100ns → ms

    return {
        "label": path.parent.parent.name,
        "frame_count": len(rows),
        "arrival_ns": arrival_ns,
        "mf_pts": mf_pts,
        "qpc_deltas_ms": qpc_deltas_ms,
        "pts_deltas_ms": pts_deltas_ms,
        "write_latency_ms": write_lat,
        "frame_sizes": sizes,
        "drops": drops,
    }


def find_frame_logs(output_dir: Path) -> list[Path]:
    """Find all frame_log.csv files under output_dir."""
    return sorted(output_dir.rglob("frame_log.csv"))


def plot(distributions: list[dict], output_path: Path):
    """Generate multi-panel timestamp analysis plot."""
    n = len(distributions)
    fig, axes = plt.subplots(3, n, figsize=(7 * n, 14), squeeze=False)
    fig.suptitle("Single-Frame Mode — Frame Timestamp Analysis",
                 fontsize=16, fontweight="bold", y=0.98)

    colors = ["#e74c3c", "#3498db"]

    for i, d in enumerate(distributions):
        label = d["label"]
        c = colors[i % len(colors)]
        n_frames = d["frame_count"]
        expected_ms = 1000.0 / 60.0  # 60 fps

        # ── Row 1: Interval histogram ────────────────────────────
        ax = axes[0][i]
        qpc = d["qpc_deltas_ms"]
        pts = d["pts_deltas_ms"]

        # Filter outliers (> 3x expected) for histogram clarity
        qpc_filt = qpc[qpc < expected_ms * 3]
        pts_filt = pts[pts < expected_ms * 3]

        bins = np.linspace(0, expected_ms * 3, 80)
        ax.hist(qpc_filt, bins=bins, alpha=0.6, color=c, label=f"QPC arrival (n={len(qpc_filt)})")
        ax.hist(pts_filt, bins=bins, alpha=0.4, color="gray", label=f"MF PTS (n={len(pts_filt)})")
        ax.axvline(expected_ms, color="green", ls="--", lw=2, label=f"Expected {expected_ms:.1f} ms")
        ax.set_title(f"{label}\nInterval Distribution (60 fps target)")
        ax.set_xlabel("Inter-frame interval (ms)")
        ax.set_ylabel("Frame count")
        ax.legend(fontsize=8)

        # ── Row 2: Interval time-series ──────────────────────────
        ax = axes[1][i]
        t = np.arange(len(qpc)) / 60.0  # approximate time axis
        ax.plot(t[:len(qpc)], qpc, alpha=0.6, color=c, linewidth=0.5, label="QPC arrival")
        ax.plot(t[:len(pts)], pts, alpha=0.4, color="gray", linewidth=0.5, label="MF PTS")
        ax.axhline(expected_ms, color="green", ls="--", lw=1.5)
        ax.set_title(f"{label}\nInterval Time-Series")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Interval (ms)")
        ax.set_ylim(0, expected_ms * 4)
        ax.legend(fontsize=8)

        # ── Row 3: Write latency + frame size ────────────────────
        ax = axes[2][i]
        ax2 = ax.twinx()
        t_full = np.arange(len(d["write_latency_ms"])) / 60.0
        ax.fill_between(t_full, d["write_latency_ms"], alpha=0.3, color=c, label="Write latency (ms)")
        ax.set_title(f"{label}\nWrite Latency & Frame Size ({n_frames} frames, {d['drops']} drops)")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Write latency (ms)", color=c)
        ax.tick_params(axis="y", labelcolor=c)

        # Frame sizes (scatter every 10th)
        step = max(1, n_frames // 500)
        ax2.scatter(t_full[::step], d["frame_sizes"][::step] / 1024, s=1, alpha=0.4, color="gray")
        ax2.set_ylabel("Frame size (KB)", color="gray")
        ax2.tick_params(axis="y", labelcolor="gray")

        # Stats box
        stats_text = (
            f"QPC: avg={np.mean(qpc):.2f}  std={np.std(qpc):.2f}  "
            f"p50={np.median(qpc):.2f}  p95={np.percentile(qpc, 95):.2f} ms\n"
            f"PTS: avg={np.mean(pts):.2f}  std={np.std(pts):.2f}  "
            f"p50={np.median(pts):.2f}  p95={np.percentile(pts, 95):.2f} ms\n"
            f"Write: avg={np.mean(d['write_latency_ms']):.3f}  "
            f"max={np.max(d['write_latency_ms']):.3f} ms  "
            f"Frame: {np.mean(d['frame_sizes'])/1024:.1f} KB avg"
        )
        ax.text(0.02, 0.98, stats_text, transform=ax.transAxes,
                fontsize=7, fontfamily="monospace", verticalalignment="top",
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.8))

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    print(f"Saved: {output_path}")
    plt.show(block=False)


def main():
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path("C:/Program Files (x86)/MotorDrive_MiceCam/output")

    logs = find_frame_logs(output_dir)
    if not logs:
        print(f"No frame_log.csv found under {output_dir}")
        return 1

    print(f"Found {len(logs)} frame log(s):")
    distributions = []
    for log_path in logs:
        print(f"  {log_path}")
        distributions.append(load_frame_log(log_path))

    out_png = Path("test_output/frame_timing_plot.png")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plot(distributions, out_png)

    # Print summary table
    print(f"\n{'='*70}")
    print(f"{'Camera':<30s} {'Frames':>7s} {'QPC avg':>9s} {'PTS avg':>9s} {'Drops':>6s}")
    print(f"{'-'*70}")
    for d in distributions:
        print(f"{d['label']:<30s} {d['frame_count']:>7d} "
              f"{np.mean(d['qpc_deltas_ms']):>8.2f}ms "
              f"{np.mean(d['pts_deltas_ms']):>8.2f}ms "
              f"{d['drops']:>6d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
