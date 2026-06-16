#!/usr/bin/env python3
"""Retiming helper for MiceCam recordings.

Reads a MiceCam sidecar JSON file, computes:

    scale = experimental_timing.duration_seconds / container_timing.duration_seconds

and remuxes the MP4 with scaled input timestamps. This corrects a video whose
container timeline plays faster or slower than the measured acquisition
timeline, without decoding or interpolating frames.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _default_ffmpeg() -> str:
    bundled = Path(__file__).resolve().parent.parent / "ffmpeg" / "ffmpeg.exe"
    if bundled.exists():
        return str(bundled)
    return "ffmpeg"


def _resolve_video_path(metadata_path: Path, video_value: str) -> Path:
    video_path = Path(video_value)
    if video_path.is_absolute():
        return video_path

    candidates = [
        Path.cwd() / video_path,
        metadata_path.parent / video_path,
        metadata_path.parent / video_path.name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def retime_recording(
    metadata_path: Path,
    output_path: Path | None = None,
    ffmpeg: str | None = None,
    force: bool = False,
) -> Path:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    video_value = metadata.get("files", {}).get("video")
    if not video_value:
        raise ValueError(f"No files.video field in {metadata_path}")

    video_path = _resolve_video_path(metadata_path, video_value)
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    exp_duration = metadata.get("experimental_timing", {}).get("duration_seconds")
    container_duration = metadata.get("container_timing", {}).get("duration_seconds")
    if not exp_duration or not container_duration:
        raise ValueError("metadata must include experimental/container durations")
    if exp_duration <= 0 or container_duration <= 0:
        raise ValueError("durations must be positive")

    scale = float(exp_duration) / float(container_duration)
    if output_path is None:
        output_path = video_path.with_name(f"{video_path.stem}_retimed.mp4")
    if output_path.exists() and not force:
        raise FileExistsError(f"{output_path} exists; pass --force to overwrite")

    cmd = [
        ffmpeg or _default_ffmpeg(),
        "-hide_banner",
        "-y" if force else "-n",
        "-itsscale",
        f"{scale:.12f}",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-avoid_negative_ts",
        "make_zero",
        str(output_path),
    ]
    print(f"video: {video_path}")
    print(f"experimental_duration: {float(exp_duration):.6f}s")
    print(f"container_duration:    {float(container_duration):.6f}s")
    print(f"scale:                 {scale:.9f}")
    print(f"output:                {output_path}")
    subprocess.run(cmd, check=True)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remux an MP4 so its container timeline matches MiceCam metadata."
    )
    parser.add_argument("metadata_json", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--ffmpeg", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    retime_recording(
        args.metadata_json,
        output_path=args.output,
        ffmpeg=args.ffmpeg,
        force=args.force,
    )


if __name__ == "__main__":
    main()
