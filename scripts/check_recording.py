#!/usr/bin/env python3
"""
Quick recording health check — print a text summary of a recording session.

Usage::

    # From SRT file
    uv run python scripts/check_recording.py recording.srt

    # From JSON metadata (faster, includes container timing)
    uv run python scripts/check_recording.py recording.json

    # Batch: check all recordings in a directory
    uv run python scripts/check_recording.py output/HD_USB_Camera/2026-06-01/

Output: a terminal-friendly health report with a stability grade.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Optional


def parse_srt_stats(srt_path: Path) -> dict:
    """Extract timing stats from a MiceCam SRT file."""
    text = srt_path.read_text(encoding="utf-8")
    wall_times: list[float] = []

    for line in text.splitlines():
        match = re.search(
            r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)",
            line,
        )
        if match:
            from datetime import datetime
            base = datetime.fromisoformat(match.group(1)).timestamp()
            nanos = int(match.group(2)) / 1e9
            wall_times.append(base + nanos)

    n = len(wall_times)
    if n < 2:
        return {"frame_count": n, "error": "not enough frames"}

    intervals = [wall_times[i] - wall_times[i - 1] for i in range(1, n)]
    median_iv = statistics.median(intervals)
    actual_fps = 1.0 / median_iv if median_iv > 0 else 0

    return {
        "frame_count": n,
        "duration_s": round(wall_times[-1] - wall_times[0], 3),
        "actual_fps": round(actual_fps, 2),
        "median_interval_ms": round(median_iv * 1000, 2),
        "p99_interval_ms": round(sorted(intervals)[int(n * 0.99) - 1] * 1000, 2),
        "max_interval_ms": round(max(intervals) * 1000, 2),
        "stdev_interval_ms": round(statistics.stdev(intervals) * 1000, 2),
        "gap_count": sum(1 for iv in intervals if iv > median_iv * 1.5),
        "severe_gap_count": sum(1 for iv in intervals if iv > median_iv * 3.0),
    }


def grade(stats: dict, target_fps: Optional[int] = None) -> tuple[str, str]:
    """Return (grade A-F, color) based on recording stability."""
    fps = stats.get("actual_fps", 0)
    target = target_fps or stats.get("target_fps") or fps
    if target == 0:
        return "?", "white"

    ratio = fps / target
    gap_rate = stats.get("gap_count", 0) / max(1, stats.get("frame_count", 1))
    severe = stats.get("severe_gap_count", 0)

    if ratio >= 0.95 and gap_rate < 0.01 and severe == 0:
        return "A", "green"
    elif ratio >= 0.85 and gap_rate < 0.03:
        return "B", "green"
    elif ratio >= 0.70 and gap_rate < 0.10:
        return "C", "yellow"
    elif ratio >= 0.50:
        return "D", "red"
    else:
        return "F", "red"


def check_file(path: Path, target_fps: Optional[int] = None) -> dict:
    """Analyze a single recording file (.srt or .json)."""
    suffix = path.suffix.lower()

    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        exp = data.get("experimental_timing", {})
        cont = data.get("container_timing", {})
        req = data.get("requested", {})
        return {
            "source": path.name,
            "type": "json",
            "frame_count": exp.get("frame_count", 0),
            "duration_s": exp.get("duration_seconds", 0),
            "actual_fps": exp.get("mean_fps", 0),
            "target_fps": req.get("fps"),
            "container_fps": cont.get("fps"),
            "container_frames": cont.get("nb_frames"),
            "resolution": f"{req.get('resolution', ['?','?'])[0]}x{req.get('resolution', ['?','?'])[1]}",
            "codec": data.get("camera", {}).get("native_codec", "?"),
        }

    if suffix == ".srt":
        stats = parse_srt_stats(path)
        stats["source"] = path.name
        stats["type"] = "srt"
        if target_fps:
            stats["target_fps"] = target_fps
        return stats

    return {"source": path.name, "error": f"unsupported file type: {suffix}"}


def print_report(results: list[dict]) -> None:
    """Print a formatted terminal report."""
    print()
    print("=" * 60)
    print("  MiceCam -- Recording Health Check")
    print("=" * 60)

    for r in results:
        if "error" in r:
            print(f"\n  [FAIL] {r['source']}: {r['error']}")
            continue

        fps = r.get("actual_fps", 0)
        target = r.get("target_fps") or fps
        g, color = grade(r, target)

        colors = {"green": "\033[92m", "yellow": "\033[93m", "red": "\033[91m", "white": "\033[0m"}
        c = colors.get(color, "")
        reset = "\033[0m"

        print(f"\n  [{g}] {r['source']}")
        print(f"  {'-' * 50}")

        if "resolution" in r:
            print(f"  Resolution:    {r['resolution']}  |  Codec: {r.get('codec', '?')}")

        print(f"  Frames:        {r.get('frame_count', '?'):,}")
        print(f"  Duration:      {r.get('duration_s', 0):.2f} s")
        print(f"  Actual FPS:    {fps:.2f}  (target: {target})")
        print(f"  Grade:         {c}{g}{reset}")

        if "container_fps" in r and r["container_fps"]:
            cfps = r["container_fps"]
            print(f"  Container FPS: {cfps:.2f}  |  Container frames: {r.get('container_frames', '?')}")

        if "median_interval_ms" in r:
            print(f"  Median intvl:  {r['median_interval_ms']} ms")
            print(f"  P99 intvl:     {r['p99_interval_ms']} ms")
            print(f"  Max intvl:     {r['max_interval_ms']} ms")
            print(f"  StdDev intvl:  {r['stdev_interval_ms']} ms")
            print(f"  Gaps (>1.5x):  {r['gap_count']}  |  Severe (>3x): {r['severe_gap_count']}")

            # Stability bar
            n = r["frame_count"]
            ratio = fps / target if target else 1
            bar_width = 40
            filled = int(bar_width * min(1, ratio))
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"  FPS ratio:     [{bar}] {ratio*100:.0f}%")

    print(f"\n{'=' * 60}")
    total_frames = sum(r.get("frame_count", 0) for r in results if "error" not in r)
    total_dur = sum(r.get("duration_s", 0) for r in results if "error" not in r)
    if total_dur > 0:
        print(f"  Total: {total_frames:,} frames across {len(results)} recording(s)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Quick recording health check for MiceCam SRT/JSON files",
    )
    parser.add_argument(
        "paths", nargs="+", type=Path,
        help=".srt file, .json metadata file, or directory of recordings",
    )
    parser.add_argument(
        "--fps", type=int, default=None,
        help="Target FPS for grading (inferred from metadata if available)",
    )
    args = parser.parse_args()

    results: list[dict] = []

    for p in args.paths:
        if p.is_dir():
            # Check all .srt files in the directory (skip empty ones)
            for f in sorted(p.glob("*.json")):
                # Prefer JSON if available (has both experimental + container timing)
                results.append(check_file(f, args.fps))
            if not any(f.suffix == ".json" for f in p.iterdir()):
                for f in sorted(p.glob("*.srt")):
                    if f.stat().st_size > 200:  # skip empty SRT (header only = 120 bytes)
                        results.append(check_file(f, args.fps))
        elif p.exists():
            results.append(check_file(p, args.fps))
        else:
            print(f"Warning: not found — {p}", file=sys.stderr)

    if not results:
        print("No recordings found.", file=sys.stderr)
        sys.exit(1)

    print_report(results)


if __name__ == "__main__":
    main()
