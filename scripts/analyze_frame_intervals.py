#!/usr/bin/env python3
"""
Analyze frame intervals from MiceCam SRT timestamp files.

Reads one or two SRT files, computes inter-frame intervals from the
nanosecond-precision wall-clock timestamps, and generates an interactive
HTML report with:

- Interval timeline (green = stable, red = gaps)
- Interval histogram
- Cumulative frame-time plot (linearity check)
- Summary statistics: actual FPS, gap count, jitter

Usage::

    # Single SRT file
    uv run python scripts/analyze_frame_intervals.py recording.srt

    # Single SRT + target FPS for gap detection thresholds
    uv run python scripts/analyze_frame_intervals.py recording.srt --fps 30

    # Two SRT files (dual-camera sync comparison)
    uv run python scripts/analyze_frame_intervals.py cam_a.srt cam_b.srt

    # Also probe the MP4 with ffprobe for ground-truth metadata
    uv run python scripts/analyze_frame_intervals.py recording.srt --mp4 recording.mp4

Output: a self-contained HTML file opened in the default browser.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent


# ── SRT parsing ────────────────────────────────────────────────────────────

def parse_srt(srt_path: Path) -> tuple[list[float], float, int]:
    """Parse a MiceCam SRT file.

    Returns (wall_times, wall_start, steady_start_ns) where *wall_times*
    is a list of absolute UTC timestamps (seconds since epoch) for each frame.
    """
    text = srt_path.read_text(encoding="utf-8")
    wall_start: float = 0.0
    steady_start: int = 0
    wall_times: list[float] = []

    for line in text.splitlines():
        if line.startswith("# wall_start (UTC):"):
            try:
                ts_str = line.split(":", 1)[1].strip()
                wall_start = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, IndexError):
                pass
        elif line.startswith("# steady_start_ns:"):
            try:
                steady_start = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
        elif line.startswith("ts="):
            # ts=2026-06-01T06:16:25.142812160  frame=1
            match = re.search(
                r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)",
                line,
            )
            if match:
                base_str, nanos_str = match.group(1), match.group(2)
                base = datetime.fromisoformat(base_str).timestamp()
                nanos = int(nanos_str) / 1e9
                wall_times.append(base + nanos)

    return wall_times, wall_start, steady_start


# ── MP4 probing ────────────────────────────────────────────────────────────

def probe_mp4(mp4_path: Path) -> dict:
    """Use ffmpeg to get ground-truth duration, frame count, and FPS."""
    from micecam.camera_manager import get_ffmpeg_path as _get_ffmpeg

    ffmpeg = _get_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-i", str(mp4_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        stderr = result.stderr
    except Exception:
        return {}

    info: dict = {}
    # Stream line: "Stream #0:0: Video: mjpeg, yuvj422p, 1280x720, 19234 kb/s, 20.08 fps, ..."
    stream_match = re.search(
        r"Stream #\d+:\d+.*?Video:\s+(\S+).*?(\d+)x(\d+).*?(\d+(?:\.\d+)?)\s*(?:fps|kb/s).*?(?:(\d+(?:\.\d+)?)\s*fps)?",
        stderr,
    )
    # Simpler: find the fps value after resolution
    fps_match = re.search(r"(\d+)x(\d+)[^,]*,\s*[^,]*,\s*(\d+(?:\.\d+)?)\s*fps", stderr)
    if fps_match:
        info["resolution"] = f"{fps_match.group(1)}x{fps_match.group(2)}"
        info["container_fps"] = float(fps_match.group(3))

    codec_match = re.search(r"Video:\s+(\S+)", stderr)
    if codec_match:
        info["codec"] = codec_match.group(1)

    # Duration line: "Duration: 00:00:53.59, start: 0.000000, bitrate: 19236 kb/s"
    dur_match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if dur_match:
        h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
        info["duration_s"] = round(h * 3600 + m * 60 + s, 3)

    bitrate_match = re.search(r"bitrate:\s+(\d+)\s*kb/s", stderr)
    if bitrate_match:
        info["bitrate_kbps"] = int(bitrate_match.group(1))

    return info


# ── Analysis ───────────────────────────────────────────────────────────────

def analyze(wall_times: list[float], target_fps: Optional[int] = None) -> dict:
    """Compute frame-interval statistics.

    Returns a dict of metrics ready for JSON serialization.
    """
    n = len(wall_times)
    if n < 2:
        return {"error": "Need at least 2 frames for analysis", "frame_count": n}

    intervals = [wall_times[i] - wall_times[i - 1] for i in range(1, n)]
    durations = [wall_times[i] - wall_times[0] for i in range(n)]

    median_interval = statistics.median(intervals)
    actual_fps = 1.0 / median_interval if median_interval > 0 else 0

    # If no target FPS provided, infer from the data
    if target_fps is None:
        target_fps = round(actual_fps)

    expected_interval = 1.0 / target_fps if target_fps > 0 else median_interval

    # Gap detection
    gap_threshold = expected_interval * 1.5
    severe_threshold = expected_interval * 3.0

    gaps = []
    severe_gaps = []
    for i, iv in enumerate(intervals):
        frame_num = i + 2  # interval[i] is between frame i+1 → frame i+2
        if iv >= severe_threshold:
            severe_gaps.append({
                "frame": frame_num,
                "interval_ms": round(iv * 1000, 2),
                "ratio": round(iv / expected_interval, 2),
                "timestamp": wall_times[i + 1],
            })
        elif iv >= gap_threshold:
            gaps.append({
                "frame": frame_num,
                "interval_ms": round(iv * 1000, 2),
                "ratio": round(iv / expected_interval, 2),
                "timestamp": wall_times[i + 1],
            })

    p99 = sorted(intervals)[int(len(intervals) * 0.99)]

    return {
        "frame_count": n,
        "duration_s": round(durations[-1], 3),
        "target_fps": target_fps,
        "actual_fps": round(actual_fps, 2),
        "expected_interval_ms": round(expected_interval * 1000, 2),
        "median_interval_ms": round(median_interval * 1000, 2),
        "p99_interval_ms": round(p99 * 1000, 2),
        "max_interval_ms": round(max(intervals) * 1000, 2),
        "min_interval_ms": round(min(intervals) * 1000, 2),
        "stdev_interval_ms": round(statistics.stdev(intervals) * 1000, 2),
        "gap_count": len(gaps),
        "severe_gap_count": len(severe_gaps),
        "dropped_frame_estimate": max(0, n - int(actual_fps * durations[-1])),
        # Data for plotting (subsample if > 10k points for browser performance)
        "intervals_ms": [round(iv * 1000, 2) for iv in intervals],
        "durations_s": [round(d, 4) for d in durations],
        "gap_indices": [
            i for i, iv in enumerate(intervals) if iv >= gap_threshold
        ],
        "severe_gap_indices": [
            i for i, iv in enumerate(intervals) if iv >= severe_threshold
        ],
        "gaps": gaps,
        "severe_gaps": severe_gaps,
    }


# ── HTML report ────────────────────────────────────────────────────────────

def generate_html(
    results: list[dict],
    labels: list[str],
    mp4_info: Optional[dict] = None,
    output_path: Optional[Path] = None,
) -> Path:
    """Generate a self-contained HTML report with Plotly.js charts."""
    if output_path is None:
        output_path = Path("frame_analysis_report.html")

    data_json = json.dumps(
        {"results": results, "labels": labels, "mp4_info": mp4_info},
        indent=2,
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MiceCam — Frame Interval Analysis</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         margin: 0; padding: 20px; background: #0d1117; color: #c9d1d9; }}
  h1 {{ color: #58a6ff; margin-bottom: 4px; }}
  .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
  .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                  gap: 12px; margin-bottom: 24px; }}
  .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                padding: 14px 18px; }}
  .stat-card .label {{ color: #8b949e; font-size: 12px; text-transform: uppercase;
                       letter-spacing: 0.5px; }}
  .stat-card .value {{ color: #e6edf3; font-size: 24px; font-weight: 700;
                       margin-top: 4px; }}
  .stat-card .value.good {{ color: #3fb950; }}
  .stat-card .value.warn {{ color: #d2991d; }}
  .stat-card .value.bad {{ color: #f85149; }}
  .chart-box {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                padding: 16px; margin-bottom: 16px; }}
  .gap-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .gap-table th {{ background: #21262d; color: #8b949e; text-align: left;
                   padding: 8px 12px; position: sticky; top: 0; }}
  .gap-table td {{ padding: 6px 12px; border-bottom: 1px solid #21262d; }}
  .gap-table tr.severe {{ background: rgba(248, 81, 73, 0.12); }}
  .gap-table tr.moderate {{ background: rgba(210, 153, 29, 0.08); }}
  .gap-table-container {{ max-height: 400px; overflow-y: auto; border-radius: 8px;
                          border: 1px solid #30363d; }}
  h3 {{ color: #e6edf3; margin: 20px 0 10px; }}
  #data {{ display: none; }}
</style>
</head>
<body>
<h1>🎥 MiceCam — Frame Interval Analysis</h1>
<div class="subtitle" id="subtitle"></div>
<div class="stats-grid" id="stats"></div>
<div class="chart-box"><div id="chart_intervals" style="height:450px"></div></div>
<div class="chart-box"><div id="chart_histogram" style="height:350px"></div></div>
<div class="chart-box"><div id="chart_cumulative" style="height:350px"></div></div>
<div id="gap_section"></div>
<div id="mp4_section"></div>
<div id="data">{data_json}</div>
<script>
{_JS_LOGIC}
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path


_JS_LOGIC = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const results = DATA.results;
const labels = DATA.labels;
const mp4_info = DATA.mp4_info;

const COLORS = ['#58a6ff', '#3fb950', '#d2991d', '#f85149', '#bc8cff'];

// ── Subtitle ──
document.getElementById('subtitle').textContent =
    labels.length === 1
        ? `SRT: ${labels[0]}  ·  ${results[0].frame_count} frames  ·  ${results[0].duration_s}s  ·  ${results[0].actual_fps} fps`
        : `Comparing ${labels.length} SRT files`;

// ── Stats cards ──
const statsDiv = document.getElementById('stats');
function card(label, value, cls) {
    return `<div class="stat-card"><div class="label">${label}</div><div class="value ${cls||''}">${value}</div></div>`;
}
results.forEach((r, i) => {
    if (r.error) {
        statsDiv.innerHTML += card(`SRT ${i+1}`, 'ERROR', 'bad');
        return;
    }
    let fpsClass = r.actual_fps >= r.target_fps * 0.85 ? 'good' :
                   r.actual_fps >= r.target_fps * 0.5 ? 'warn' : 'bad';
    statsDiv.innerHTML +=
        card(`${labels[i]} — Actual FPS`,
             r.actual_fps.toFixed(1), fpsClass) +
        card(`Target FPS`, r.target_fps) +
        card(`Median Interval`, r.median_interval_ms.toFixed(2) + ' ms') +
        card(`P99 Interval`, r.p99_interval_ms.toFixed(2) + ' ms') +
        card(`Max Interval`, r.max_interval_ms.toFixed(2) + ' ms') +
        card(`Std Dev`, r.stdev_interval_ms.toFixed(2) + ' ms') +
        card(`Gaps (1.5x)`, r.gap_count, r.gap_count > r.frame_count * 0.05 ? 'bad' : 'good') +
        card(`Severe Gaps (3x)`, r.severe_gap_count, r.severe_gap_count > 0 ? 'bad' : 'good') +
        card(`Est. Drops`, r.dropped_frame_estimate,
             r.dropped_frame_estimate > r.frame_count * 0.1 ? 'bad' : '');
});

// ── Chart 1: Frame interval over time ──
(function() {
    const traces = [];
    results.forEach((r, idx) => {
        if (r.error || !r.intervals_ms) return;
        const color = COLORS[idx % COLORS.length];
        const n = r.intervals_ms.length;
        const x = Array.from({length: n}, (_, i) => i + 2);  // frame numbers
        const y = r.intervals_ms;
        const exp = r.expected_interval_ms;
        const sev = exp * 3;

        // Scatter: all points (small, semi-transparent)
        traces.push({
            x: x, y: y,
            type: 'scatter', mode: 'markers',
            name: `${labels[idx]} (normal)`,
            marker: { color: color, size: 2, opacity: 0.5 },
            legendgroup: labels[idx],
            showlegend: false,
        });

        // Scatter: severe gaps (large, red)
        if (r.severe_gap_indices.length > 0) {
            const gx = r.severe_gap_indices.map(i => i + 2);
            const gy = r.severe_gap_indices.map(i => y[i]);
            traces.push({
                x: gx, y: gy,
                type: 'scatter', mode: 'markers',
                name: `${labels[idx]} (severe gap)`,
                marker: { color: '#f85149', size: 6, symbol: 'x' },
                legendgroup: labels[idx],
            });
        }

        // Moderate gaps
        if (r.gap_indices.length > 0) {
            const mgx = r.gap_indices.map(i => i + 2);
            const mgy = r.gap_indices.map(i => y[i]);
            traces.push({
                x: mgx, y: mgy,
                type: 'scatter', mode: 'markers',
                name: `${labels[idx]} (gap)`,
                marker: { color: '#d2991d', size: 4, symbol: 'triangle-up' },
                legendgroup: labels[idx],
            });
        }

        // Expected interval line
        traces.push({
            x: [x[0], x[x.length - 1]],
            y: [exp, exp],
            type: 'scatter', mode: 'lines',
            name: `Expected (${r.target_fps} fps)`,
            line: { dash: 'dash', color: color, width: 1, opacity: 0.5 },
            showlegend: idx === 0,
        });
    });

    Plotly.newPlot('chart_intervals', traces, {
        title: { text: 'Frame Interval Over Time', font: { color: '#e6edf3' } },
        xaxis: { title: 'Frame Number', gridcolor: '#21262d', color: '#8b949e' },
        yaxis: { title: 'Interval (ms)', gridcolor: '#21262d', color: '#8b949e',
                 type: 'log' },
        legend: { font: { color: '#8b949e' } },
        paper_bgcolor: '#161b22', plot_bgcolor: '#161b22',
        margin: { l: 60, r: 20, t: 40, b: 50 },
    }, { responsive: true, displaylogo: false });
})();

// ── Chart 2: Histogram ──
(function() {
    const traces = results.filter(r => !r.error && r.intervals_ms).map((r, idx) => ({
        x: r.intervals_ms,
        type: 'histogram',
        name: labels[idx],
        marker: { color: COLORS[idx % COLORS.length], opacity: 0.7 },
        xbins: { size: r.expected_interval_ms * 0.15 },
    }));
    Plotly.newPlot('chart_histogram', traces, {
        title: { text: 'Frame Interval Distribution', font: { color: '#e6edf3' } },
        xaxis: { title: 'Interval (ms)', gridcolor: '#21262d', color: '#8b949e' },
        yaxis: { title: 'Count', gridcolor: '#21262d', color: '#8b949e' },
        barmode: 'overlay',
        legend: { font: { color: '#8b949e' } },
        paper_bgcolor: '#161b22', plot_bgcolor: '#161b22',
        margin: { l: 60, r: 20, t: 40, b: 50 },
    }, { responsive: true, displaylogo: false });
})();

// ── Chart 3: Cumulative frame-time ──
(function() {
    const traces = [];
    results.forEach((r, idx) => {
        if (r.error || !r.durations_s) return;
        const n = r.durations_s.length;
        const x = r.durations_s;
        const y = Array.from({length: n}, (_, i) => i + 1);

        traces.push({
            x: x, y: y,
            type: 'scatter', mode: 'lines',
            name: labels[idx],
            line: { color: COLORS[idx % COLORS.length], width: 2 },
        });

        // Ideal line: y = fps * x
        const maxT = x[n - 1];
        traces.push({
            x: [0, maxT],
            y: [0, r.target_fps * maxT],
            type: 'scatter', mode: 'lines',
            name: `Ideal (${r.target_fps} fps)`,
            line: { dash: 'dot', color: '#8b949e', width: 1 },
            showlegend: idx === 0,
        });
    });

    Plotly.newPlot('chart_cumulative', traces, {
        title: { text: 'Cumulative Frames vs Wall-Clock Time', font: { color: '#e6edf3' } },
        xaxis: { title: 'Elapsed Time (s)', gridcolor: '#21262d', color: '#8b949e' },
        yaxis: { title: 'Frame Count', gridcolor: '#21262d', color: '#8b949e' },
        legend: { font: { color: '#8b949e' } },
        paper_bgcolor: '#161b22', plot_bgcolor: '#161b22',
        margin: { l: 60, r: 20, t: 40, b: 50 },
    }, { responsive: true, displaylogo: false });
})();

// ── Gap tables ──
(function() {
    let html = '';
    results.forEach((r, i) => {
        if (r.error) return;
        const allGaps = [...(r.severe_gaps || []), ...(r.gaps || [])]
            .sort((a, b) => b.interval_ms - a.interval_ms);
        if (allGaps.length === 0) {
            html += `<h3>📊 ${labels[i]} — No Gaps Detected ✅</h3>`;
            return;
        }
        html += `<h3>📊 ${labels[i]} — ${allGaps.length} Gap(s) Detected</h3>`;
        html += `<div class="gap-table-container"><table class="gap-table">`;
        html += `<tr><th>Frame #</th><th>Interval (ms)</th><th>Ratio</th><th>Severity</th><th>Timestamp (UTC)</th></tr>`;
        allGaps.forEach(g => {
            const sev = g.ratio >= 3 ? 'severe' : 'moderate';
            const label = g.ratio >= 3 ? '🔴 SEVERE' : '🟡 GAP';
            html += `<tr class="${sev}"><td>${g.frame}</td><td>${g.interval_ms}</td>`;
            html += `<td>${g.ratio}x</td><td>${label}</td>`;
            html += `<td>${new Date(g.timestamp * 1000).toISOString()}</td></tr>`;
        });
        html += `</table></div>`;
    });
    document.getElementById('gap_section').innerHTML = html;
})();

// ── MP4 metadata ──
if (mp4_info && Object.keys(mp4_info).length > 0) {
    const s = mp4_info;
    let html = `<h3>📦 MP4 Container Metadata (ffprobe)</h3>`;
    html += `<div class="stats-grid">`;
    html += card('Codec', s.codec || '?');
    html += card('Resolution', s.resolution || '?');
    html += card('Container FPS', s.container_fps ? s.container_fps.toFixed(2) : '?');
    html += card('Duration', s.duration_s ? s.duration_s.toFixed(2) + ' s' : '?');
    html += card('Bitrate', s.bitrate_kbps ? s.bitrate_kbps + ' kbps' : '?');
    html += card('Frames (metadata)', s.nb_frames || '?');
    html += `</div>`;
    document.getElementById('mp4_section').innerHTML = html;
}

// ── Responsive resize ──
window.addEventListener('resize', () => {
    ['chart_intervals', 'chart_histogram', 'chart_cumulative'].forEach(id => {
        Plotly.Plots.resize(document.getElementById(id));
    });
});
"""


# ── Auto-discovery ──────────────────────────────────────────────────────────

def _find_latest_recordings(
    directory: Path, count: int = 1,
) -> list[tuple[Path, Optional[Path]]]:
    """Find the most recent .srt files (with paired .mp4 if present).

    Returns list of (srt_path, mp4_path_or_None), newest first.
    Skips .srt files that are header-only (<= 200 bytes).
    """
    pairs: list[tuple[Path, Optional[Path], float]] = []  # (srt, mp4, mtime)

    for srt in sorted(directory.glob("*.srt"), key=lambda p: -p.stat().st_mtime):
        if srt.stat().st_size <= 200:
            continue  # header-only stub
        stem = srt.stem
        mp4 = directory / f"{stem}.mp4"
        pairs.append((srt, mp4 if mp4.exists() else None, srt.stat().st_mtime))

    # Return newest first
    pairs.sort(key=lambda x: -x[2])
    return [(srt, mp4) for srt, mp4, _ in pairs[:count]]


def _infer_target_fps_from_srt(srt_path: Path) -> Optional[int]:
    """Try to infer target FPS from the SRT file's comment header."""
    text = srt_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("# target_fps:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
    return None


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyze MiceCam SRT frame intervals",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s                          # auto-detect latest recording in cwd
  %(prog)s output/HD_USB_Camera/    # auto-detect latest in directory
  %(prog)s rec.srt --mp4 rec.mp4    # explicit files
  %(prog)s cam_a.srt cam_b.srt      # compare two recordings
  %(prog)s --last 3                 # compare last 3 recordings""",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help=".srt file(s), a directory to scan, or nothing (auto-detect in cwd)",
    )
    parser.add_argument(
        "--fps", type=int, default=None,
        help="Target recording FPS (auto-detected from SRT header or data if omitted)",
    )
    parser.add_argument(
        "--mp4", type=Path, default=None,
        help="Corresponding .mp4 file (only with a single explicit .srt)",
    )
    parser.add_argument(
        "--last", type=int, default=None, metavar="N",
        help="Compare the N most recent recordings (default: 1)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output HTML path (default: frame_analysis_report.html)",
    )
    args = parser.parse_args()

    # ── Resolve input files ──
    srt_files: list[Path] = []
    mp4_file: Optional[Path] = args.mp4

    if not args.paths:
        # No args: auto-detect latest in cwd
        cwd = Path.cwd()
        print(f"Auto-detecting latest recording in: {cwd}")
        pairs = _find_latest_recordings(cwd, count=args.last or 1)
        if not pairs:
            # Try the default output directory
            default = cwd / "output"
            if default.exists():
                # Recurse into date subdirs
                for subdir in sorted(default.rglob("*.srt"), key=lambda p: -p.stat().st_mtime):
                    parent = subdir.parent
                    print(f"Auto-detecting in: {parent}")
                    pairs = _find_latest_recordings(parent, count=args.last or 1)
                    if pairs:
                        break
        if not pairs:
            print("Error: no .srt files found in current directory or output/",
                  file=sys.stderr)
            sys.exit(1)
        for srt, mp4 in pairs:
            srt_files.append(srt)
            if mp4 and mp4_file is None:
                mp4_file = mp4

    elif len(args.paths) == 1 and args.paths[0].is_dir():
        # Directory: auto-detect latest
        directory = args.paths[0]
        print(f"Auto-detecting latest recording in: {directory}")
        pairs = _find_latest_recordings(directory, count=args.last or 1)
        if not pairs:
            print(f"Error: no .srt files found in {directory}", file=sys.stderr)
            sys.exit(1)
        for srt, mp4 in pairs:
            srt_files.append(srt)
            if mp4 and mp4_file is None:
                mp4_file = mp4

    else:
        # Explicit files
        for p in args.paths:
            if p.is_dir():
                print(f"Warning: skipping directory {p} (use without other args to scan)",
                      file=sys.stderr)
                continue
            if p.suffix.lower() == ".mp4":
                if mp4_file is None:
                    mp4_file = p
                continue
            srt_files.append(p)

    if not srt_files:
        print("Error: no .srt files specified or found", file=sys.stderr)
        sys.exit(1)

    if len(srt_files) > 5:
        print(f"Limiting to newest 5 of {len(srt_files)} found", file=sys.stderr)
        srt_files = srt_files[:5]

    # ── Analyze ──
    results = []
    labels = []
    for p in srt_files:
        if not p.exists():
            print(f"Warning: file not found: {p}", file=sys.stderr)
            continue
        wall_times, _wall_start, _steady = parse_srt(p)
        print(f"Parsed {p.name}: {len(wall_times)} frames", end="")

        # Auto-infer target FPS
        fps = args.fps
        if fps is None:
            fps = _infer_target_fps_from_srt(p)
        if fps is None and wall_times and len(wall_times) >= 2:
            # Fallback: round actual median FPS to nearest common value
            intervals = [wall_times[i] - wall_times[i - 1] for i in range(1, len(wall_times))]
            median_fps = 1.0 / statistics.median(intervals) if intervals else 0
            for candidate in [120, 60, 50, 30, 25, 24, 15, 10]:
                if abs(median_fps - candidate) < candidate * 0.25:
                    fps = candidate
                    break
            if fps is None:
                fps = round(median_fps)
        print(f"  (target: {fps} fps)")

        r = analyze(wall_times, target_fps=fps)
        results.append(r)
        labels.append(p.name)

    # ── MP4 probe ──
    mp4_info = None
    if mp4_file:
        if mp4_file.exists():
            print(f"Probing {mp4_file.name}...")
            mp4_info = probe_mp4(mp4_file)
            if mp4_info:
                print(f"  Container: {mp4_info.get('codec','?')} "
                      f"{mp4_info.get('resolution','?')} "
                      f"{mp4_info.get('container_fps','?')}fps "
                      f"{mp4_info.get('duration_s','?')}s")
            else:
                print("  (no data)")
        else:
            print(f"Warning: MP4 not found: {mp4_file}")

    # ── Output ──
    output_path = args.output or Path("frame_analysis_report.html")
    report_path = generate_html(results, labels, mp4_info, output_path)
    print(f"\nReport: {report_path.resolve()}")

    webbrowser.open(str(report_path.resolve()))
    print("Opened in browser.")


if __name__ == "__main__":
    main()
