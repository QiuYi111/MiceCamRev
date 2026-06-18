"""
Unpack frames from a single-frame-mode frames.bin archive.

Usage:
    uv run python scripts/unpack_frames.py <metadata.json>
    uv run python scripts/unpack_frames.py <metadata.json> --frame 42
    uv run python scripts/unpack_frames.py <metadata.json> --start 0 --end 99
    uv run python scripts/unpack_frames.py <metadata.json> --output-dir ./extracted
"""

import csv
import json
import sys
from pathlib import Path

# Map capture format to file extension
FORMAT_EXT = {
    "mjpg": ".jpg",
    "mjpeg": ".jpg",
    "yuy2": ".yuv",
    "yuyv422": ".yuv",
    "nv12": ".yuv",
    "rgb24": ".rgb",
    "h264": ".h264",
}


def load_index(index_path: Path) -> list[dict]:
    """Read frames.idx.csv, return list of {frame_id, offset, bytes, ...}."""
    rows = []
    with open(index_path, "r") as f:
        for r in csv.DictReader(f):
            rows.append({
                "frame_id": int(r["frame_id"]),
                "offset": int(r["offset"]),
                "size": int(r["bytes"]),
                "arrival_qpc_ns": int(r["arrival_qpc_ns"]),
                "mf_pts_100ns": int(r["mf_pts_100ns"]),
                "width": int(r["width"]),
                "height": int(r["height"]),
                "format": r.get("format", "mjpg"),
            })
    return rows


def extract_frame(bin_path: Path, entry: dict, output_dir: Path) -> Path:
    """Extract a single frame from the bin file.  Returns the output path."""
    ext = FORMAT_EXT.get(entry["format"], ".bin")
    out_name = f"frame_{entry['frame_id']:06d}{ext}"
    out_path = output_dir / out_name
    with open(bin_path, "rb") as src:
        src.seek(entry["offset"])
        data = src.read(entry["size"])
    out_path.write_bytes(data)
    return out_path


def unpack(
    session_dir: Path,
    output_dir: Path,
    start: int | None = None,
    end: int | None = None,
    frame_id: int | None = None,
) -> tuple[int, Path]:
    """Extract frames from a session directory.  Returns (count, output_dir)."""
    metadata_path = session_dir / "metadata.json"
    bin_path = session_dir / "frames.bin"
    idx_path = session_dir / "frames.idx.csv"

    if not bin_path.exists():
        raise FileNotFoundError(f"frames.bin not found: {bin_path}")
    if not idx_path.exists():
        raise FileNotFoundError(f"frames.idx.csv not found: {idx_path}")

    entries = load_index(idx_path)
    if not entries:
        print("No frames in index — nothing to extract.")
        return (0, output_dir)

    # ── Filter ──────────────────────────────────────────────────────
    if frame_id is not None:
        entries = [e for e in entries if e["frame_id"] == frame_id]
        if not entries:
            print(f"Frame {frame_id} not found in index.")
            return (0, output_dir)
    elif start is not None or end is not None:
        start = start if start is not None else 0
        end = end if end is not None else entries[-1]["frame_id"]
        entries = [e for e in entries if start <= e["frame_id"] <= end]

    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(entries)

    print(f"Extracting {total} frame(s) from {bin_path.name} ({bin_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"Output: {output_dir}")

    for i, entry in enumerate(entries):
        out_path = extract_frame(bin_path, entry, output_dir)
        if (i + 1) % max(1, total // 20) == 0:
            print(f"  {i + 1}/{total} ... {out_path.name}")

    total_kb = sum(e["size"] for e in entries) / 1024
    print(f"Done. {total} frames, {total_kb:.0f} KB total.")
    return (total, output_dir)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    target = Path(sys.argv[1])

    # Accept either metadata.json or the session directory
    if target.is_file() and target.name == "metadata.json":
        session_dir = target.parent
    elif target.is_dir():
        session_dir = target
    else:
        print(f"Not a valid session: {target}")
        return 1

    # Parse optional arguments
    frame_id: int | None = None
    start: int | None = None
    end: int | None = None
    output_dir: Path = session_dir / "extracted"

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--frame" and i + 1 < len(args):
            frame_id = int(args[i + 1]); i += 2
        elif args[i] == "--start" and i + 1 < len(args):
            start = int(args[i + 1]); i += 2
        elif args[i] == "--end" and i + 1 < len(args):
            end = int(args[i + 1]); i += 2
        elif args[i] == "--output-dir" and i + 1 < len(args):
            output_dir = Path(args[i + 1]); i += 2
        else:
            i += 1

    try:
        count, out = unpack(session_dir, output_dir, start, end, frame_id)
        if count == 0:
            return 1
    except Exception as exc:
        print(f"Error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
