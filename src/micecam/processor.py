"""
Post-recording pipeline: decode → analyze → align → burn → encode.

Adapted from VideoInspect/motorvids.py with added unified-experiment-directory
conventions.  Works with both ``bin``-backed and ``single``-backed recordings.
"""

from __future__ import annotations

import csv as _csv
import json
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import mean

import numpy as np

CST = timezone(timedelta(hours=8))


# ══════════════════════════════════════════════════════
# FrameSource — reads a *_single_frames/ directory
# ══════════════════════════════════════════════════════

class FrameSource:
    """Reads a single-frame recording (bin-backed or JPEG-backed)."""

    def __init__(self, sf_dir: Path):
        self.dir = Path(sf_dir)

    @property
    def name(self) -> str:
        return self.dir.name.replace("_single_frames", "")

    @property
    def meta(self) -> dict:
        with open(self.dir / "metadata.json", encoding="utf-8") as f:
            return json.load(f)

    @property
    def n_frames(self) -> int:
        return self.meta["diagnostics"]["frame_count"]

    @property
    def wall_start(self) -> float:
        return self.meta["shared_time_base"]["wall_start"]

    @property
    def steady_start_ns(self) -> int:
        return self.meta["shared_time_base"]["steady_start_ns"]

    @property
    def nominal_fps(self) -> float:
        return float(self.meta["capture"]["fps"])

    @property
    def is_bin(self) -> bool:
        return (self.dir / "frames.bin").exists()

    def load_log(self) -> list[dict]:
        """Return per-frame dicts with frame_id, arrival_qpc_ns, arrival_delta_ms, ..."""
        idx_path = self.dir / "frames.idx.csv"
        if idx_path.exists():
            with open(idx_path, encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
            arr_ns = [int(r["arrival_qpc_ns"]) for r in rows]
            for i, r in enumerate(rows):
                r["frame_id"] = int(r["frame_id"])
                r["arrival_qpc_ns"] = arr_ns[i]
                r["arrival_delta_ms"] = (arr_ns[i] - arr_ns[i - 1]) / 1e6 if i > 0 else 0.0
                r["mf_pts_100ns"] = int(r.get("mf_pts_100ns", 0))
                r["mf_pts_delta_ms"] = (int(r.get("mf_pts_100ns", 0)) - int(rows[i - 1].get("mf_pts_100ns", 0))) / 10000.0 if i > 0 else 0.0
            return rows

        log_path = self.dir / "frame_log.csv"
        if log_path.exists():
            with open(log_path, encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
            for r in rows:
                r["frame_id"] = int(r["frame_id"])
                r["arrival_qpc_ns"] = int(r.get("arrival_qpc_ns", 0))
                r["arrival_delta_ms"] = float(r.get("arrival_delta_ms", 0))
            return rows

        raise FileNotFoundError(f"No frame_log.csv or frames.idx.csv in {self.dir}")

    def frame_epochs(self) -> np.ndarray:
        log = self.load_log()
        ws, ss = self.wall_start, self.steady_start_ns
        return np.array([ws + (r["arrival_qpc_ns"] - ss) / 1e9 for r in log])

    def frame_nums(self) -> np.ndarray:
        return np.array([r["frame_id"] for r in self.load_log()])

    def read_jpeg(self, frame_id: int) -> bytes:
        if self.is_bin:
            with open(self.dir / "frames.idx.csv", encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    if int(row["frame_id"]) == frame_id:
                        with open(self.dir / "frames.bin", "rb") as bf:
                            bf.seek(int(row["offset"]))
                            return bf.read(int(row["bytes"]))
            raise ValueError(f"Frame {frame_id} not in bin")
        return (self.dir / "frames" / f"frame_{frame_id:06d}.jpg").read_bytes()


# ══════════════════════════════════════════════════════
# Experiment — reads MotorDrive mice_*.json
# ══════════════════════════════════════════════════════

class Experiment:
    """Loads a MotorDrive experiment JSON — provides IMU + motor alignment data."""

    def __init__(self, path: Path):
        if path.is_dir():
            candidates = sorted(path.glob("mice_*.json")) + sorted(path.glob("experiment.json"))
            if not candidates:
                raise FileNotFoundError(f"No experiment JSON in {path}")
            path = candidates[0]
        with open(path, encoding="utf-8") as f:
            self._data = json.load(f)
        self._records: list[dict] | None = None

    @property
    def name(self) -> str:
        return self._data.get("experiment", {}).get("name", "unknown")

    @property
    def n_trials(self) -> int:
        return self._data.get("summary", {}).get("total_trials", 0)

    @property
    def has_imu(self) -> bool:
        """True if the experiment contains IMU (yaw/gyro) data."""
        recs = self.records()
        return any(r.get("yaw_deg") for r in recs[:100])

    @property
    def has_motor(self) -> bool:
        """True if the experiment has motor-reported (speed/angle) data."""
        recs = self.records()
        return any(r.get("motor_speed") for r in recs[:100])

    def records(self) -> list[dict]:
        """Return all per-sample records with both IMU and motor fields."""
        if self._records is not None:
            return self._records
        records = []
        for trial in self._data.get("trials", []):
            for phase_key in ("forward", "reverse"):
                for s in trial.get(phase_key, {}).get("samples", []):
                    dt = datetime.fromisoformat(s["time"]).replace(tzinfo=CST)
                    records.append(dict(
                        epoch=dt.timestamp(),
                        # IMU fields
                        yaw_deg=s.get("yaw_deg", 0),
                        gyro_rpm=s.get("gyro_rpm", 0),
                        imu_gz=s.get("imu_gz", 0),
                        # Motor-reported fields
                        motor_speed=s.get("motor_speed", 0),
                        motor_angle=s.get("motor_angle", 0),
                        motor_pulses=s.get("motor_pulses", 0),
                        motor_running=s.get("motor_running", False),
                        trial_id=trial["trial_id"],
                    ))
        records.sort(key=lambda r: r["epoch"])
        self._records = records
        return records

    # backward-compat alias
    def imu_records(self) -> list[dict]:
        return self.records()

    def match(self, frame_epochs: np.ndarray, frame_nums: np.ndarray) -> dict:
        """Return dict[frame_num] = imu_record|None for nearest-neighbour alignment."""
        recs = self.imu_records()
        rec_ep = np.array([r["epoch"] for r in recs])
        n = len(recs)
        data = {}
        cur = 0
        for fn, fe in zip(frame_nums, frame_epochs):
            while cur + 1 < n and rec_ep[cur + 1] <= fe:
                cur += 1
            if cur + 1 < n:
                d0, d1 = abs(rec_ep[cur] - fe), abs(rec_ep[cur + 1] - fe)
                best = cur if d0 <= d1 else cur + 1
            else:
                best = cur
            data[int(fn)] = recs[best] if abs(recs[best]["epoch"] - fe) < 0.1 else None
        return data


# ══════════════════════════════════════════════════════
# Discovery
# ══════════════════════════════════════════════════════

def find_sources(path: Path) -> list[FrameSource]:
    """Find all *_single_frames directories under *path*."""
    if path.is_dir():
        meta = path / "metadata.json"
        if meta.exists() and ((path / "frame_log.csv").exists() or (path / "frames.idx.csv").exists()):
            return [FrameSource(path)]

    seen = set()
    result = []
    for d in sorted(path.rglob("*_single_frames")):
        key = d.resolve()
        if key not in seen:
            seen.add(key)
            result.append(FrameSource(d))
    for d in sorted(path.rglob("metadata.json")):
        parent = d.parent
        key = parent.resolve()
        if key not in seen and ((parent / "frame_log.csv").exists() or (parent / "frames.idx.csv").exists()):
            seen.add(key)
            result.append(FrameSource(parent))
    return result


# ══════════════════════════════════════════════════════
# Pipeline steps  (callable, log-friendly)
# ══════════════════════════════════════════════════════

def step_decode(src: FrameSource, out_dir: Path, log_cb=None) -> int:
    """Extract JPEGs from bin or copy from single.  Returns frame count."""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    meta = src.meta
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    log = src.load_log()
    with open(out_dir / "frame_log.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["frame_id", "arrival_qpc_ns", "arrival_delta_ms", "mf_pts_100ns",
                     "mf_pts_delta_ms", "bytes", "width", "height", "format"])
        for r in log:
            w.writerow([r["frame_id"], r.get("arrival_qpc_ns", ""),
                        f"{r.get('arrival_delta_ms', 0):.6f}",
                        r.get("mf_pts_100ns", ""),
                        f"{r.get('mf_pts_delta_ms', 0):.6f}",
                        r.get("bytes", ""), r.get("width", ""), r.get("height", ""),
                        r.get("format", "")])

    existing = {int(f.stem.split("_")[1]) for f in frames_dir.glob("*.jpg")}
    n = 0
    if src.is_bin:
        idx_path = src.dir / "frames.idx.csv"
        with open(idx_path, encoding="utf-8") as f:
            idx_rows = list(_csv.DictReader(f))
        with open(src.dir / "frames.bin", "rb") as bf:
            for row in idx_rows:
                fn = int(row["frame_id"])
                if fn not in existing:
                    bf.seek(int(row["offset"]))
                    (frames_dir / f"frame_{fn:06d}.jpg").write_bytes(bf.read(int(row["bytes"])))
                    n += 1
    else:
        src_frames = src.dir / "frames"
        for fn in [r["frame_id"] for r in log]:
            if fn not in existing:
                shutil.copy2(src_frames / f"frame_{fn:06d}.jpg", frames_dir / f"frame_{fn:06d}.jpg")
                n += 1
    total = len(list(frames_dir.glob("*.jpg")))
    if log_cb:
        log_cb(f"Decode: {src.name}: {n} extracted, {total} total JPEGs → {out_dir}")
    return total


def step_analyze(src: FrameSource, out_dir: Path, log_cb=None) -> dict:
    """Compute frame-interval statistics + plots.  Returns stats dict."""
    out_dir.mkdir(parents=True, exist_ok=True)

    log = src.load_log()
    arrival_ns = np.array([r["arrival_qpc_ns"] for r in log])
    deltas_ms = np.array([r["arrival_delta_ms"] for r in log[1:]])
    frames = len(log)
    n_drop = int((deltas_ms > 17).sum())

    stats = {
        "name": src.name, "n_frames": frames,
        "fps_nom": src.nominal_fps,
        "fps_actual": float(frames / (src.frame_epochs()[-1] - src.frame_epochs()[0])),
        "interval_mean_ms": float(deltas_ms.mean()),
        "interval_median_ms": float(np.median(deltas_ms)),
        "interval_std_ms": float(deltas_ms.std()),
        "cv_pct": float(deltas_ms.std() / deltas_ms.mean() * 100),
        "drops": n_drop,
        "drop_pct": float(n_drop / max(1, len(deltas_ms)) * 100),
    }
    with open(out_dir / "analysis.json", "w") as f:
        json.dump(stats, f, indent=2)

    if log_cb:
        log_cb(f"Analyze: {src.name}: {frames}f "
               f"avg={stats['interval_mean_ms']:.2f}ms "
               f"CV={stats['cv_pct']:.1f}% drops={n_drop}")

    # Plot
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        t0 = arrival_ns[0]
        t_min = (arrival_ns - t0) / 1e9 / 60
        step = max(1, len(deltas_ms) // 20000)

        fig, axes = plt.subplots(2, 2, figsize=(18, 10))
        c = "#1f77b4"
        axes[0, 0].scatter(t_min[1:][::step], deltas_ms[::step], s=0.3, alpha=0.5, color=c, rasterized=True)
        axes[0, 0].axhline(y=1000 / src.nominal_fps, color="red", linestyle="--", alpha=0.5)
        axes[0, 0].set(xlabel="Time (min)", ylabel="Interval (ms)",
                        title="Frame Interval Over Time"); axes[0, 0].grid(True, alpha=0.3)

        axes[0, 1].hist(deltas_ms, bins=120, alpha=0.7, color=c)
        axes[0, 1].axvline(x=1000 / src.nominal_fps, color="red", linestyle="--")
        axes[0, 1].set_xlim(0, 60)
        axes[0, 1].set(xlabel="Interval (ms)", ylabel="Count",
                        title="Frame Interval Distribution"); axes[0, 1].grid(True, alpha=0.3)

        drop = deltas_ms > 17
        axes[1, 0].scatter(t_min[1:][drop], deltas_ms[drop], s=2, alpha=0.6,
                            color="red", rasterized=True)
        axes[1, 0].axhline(y=16.67, color="gray", linestyle="--", alpha=0.3)
        axes[1, 0].set(xlabel="Time (min)", ylabel="Drop Interval (ms)",
                        title=f"Drop Events (>17ms) — {drop.sum()} total")
        axes[1, 0].grid(True, alpha=0.3)

        bins_1m = np.arange(0, t_min[-1] + 1, 1)
        dr, _ = np.histogram(t_min[1:][drop], bins=bins_1m)
        axes[1, 1].bar(bins_1m[:-1], dr, width=0.8, color=c, alpha=0.7)
        axes[1, 1].set(xlabel="Time (min)", ylabel="Drops/min",
                        title="Drop Rate Over Time"); axes[1, 1].grid(True, alpha=0.3)

        fig.tight_layout()
        fig.savefig(out_dir / "analysis.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
    except ImportError:
        if log_cb:
            log_cb("  (matplotlib not available — skipping plot)")

    return stats


def step_align(src: FrameSource, exp: Experiment, out_dir: Path, log_cb=None) -> int:
    """Match camera frames to IMU records.  Returns matched count."""
    out_dir.mkdir(parents=True, exist_ok=True)

    epochs = src.frame_epochs()
    nums = src.frame_nums()
    data = exp.match(epochs, nums)

    n_matched = sum(1 for v in data.values() if v is not None)
    if log_cb:
        log_cb(f"Align: {src.name}: {n_matched}/{src.n_frames} matched "
               f"({n_matched / max(1, src.n_frames) * 100:.1f}%)")

    fieldnames = ["frame_id", "epoch",
                  "yaw_deg", "gyro_rpm", "imu_gz",
                  "motor_speed", "motor_angle", "motor_pulses",
                  "motor_running", "trial_id"]
    with open(out_dir / "match.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for fn, r in data.items():
            row = {"frame_id": fn, "epoch": ""}
            if r is not None:
                row.update({k: r.get(k, "") for k in fieldnames[1:]})
            w.writerow(row)
    return n_matched


def _load_match(match_path: Path) -> dict:
    data = {}
    with open(match_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            fid = int(row["frame_id"])
            if row["trial_id"]:
                data[fid] = {
                    # IMU
                    "yaw_deg": float(row.get("yaw_deg", 0)),
                    "gyro_rpm": float(row.get("gyro_rpm", 0)),
                    "imu_gz": float(row.get("imu_gz", 0)),
                    # Motor
                    "motor_speed": float(row.get("motor_speed", 0)),
                    "motor_angle": float(row.get("motor_angle", 0)),
                    "motor_pulses": float(row.get("motor_pulses", 0)),
                    "motor_running": row.get("motor_running", "") == "True",
                    "trial_id": int(row["trial_id"]),
                }
            else:
                data[fid] = None
    return data


def _resample_30fps(arrival_ns: np.ndarray) -> list[int]:
    deltas = np.diff(arrival_ns) / 1e6
    target, big = 30.0, 20.0
    accum, kept = 0.0, [0]
    for i in range(1, len(arrival_ns)):
        gap = deltas[i - 1]
        if gap > big:
            if accum > 0 and kept[-1] != i - 1:
                kept.append(i - 1)
            kept.append(i)
            accum = 0
        else:
            accum += gap
            if accum >= target:
                kept.append(i)
                accum = 0
    return kept


def _get_font():
    from PIL import ImageFont
    for fp in ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/Cour.ttf",
               "C:/Windows/Fonts/lucon.ttf"]:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, 22)
            except Exception:
                continue
    return ImageFont.load_default()


def _burn_one(src: Path, data: dict | None, dst: Path, font,
              annotation: str = "imu") -> bool:
    """Burn overlay onto one frame.  *annotation* selects IMU or motor fields."""
    from PIL import Image, ImageDraw
    if data is None:
        if not dst.exists():
            shutil.copy2(str(src), str(dst))
        return False
    try:
        img = Image.open(str(src))
        running = data.get("motor_running", False)
        sc = (0, 255, 0) if running else (128, 128, 128)
        st = "RUN" if running else "IDLE"

        if annotation == "motor":
            speed = float(data.get("motor_speed", 0))
            angle = float(data.get("motor_angle", 0))
            pulses = float(data.get("motor_pulses", 0))
            ac = (0, 255, 255) if abs(speed) > 10 else ((0, 255, 0) if abs(speed) > 1 else (128, 128, 128))
            line1 = (f"Trial {data['trial_id']:>2}  |  Speed: {speed:+7.1f} RPM  |  "
                     f"Angle: {angle:8.2f}°")
            line2 = f"Pulses: {pulses:7.0f}  |  {st}"
            lc = ac
        else:
            yaw = float(data.get("yaw_deg", 0))
            rpm = float(data.get("gyro_rpm", 0))
            gz = float(data.get("imu_gz", 0))
            gc = (0, 255, 255) if abs(gz) > 10 else ((0, 255, 0) if abs(gz) > 1 else (128, 128, 128))
            line1 = (f"Trial {data['trial_id']:>2}  |  Yaw: {yaw:7.2f}°  |  "
                     f"RPM: {rpm:+6.1f}")
            line2 = f"Gz: {gz:+7.1f} deg/s  |  {st}"
            lc = gc

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([(10, 5), (620, 65)], fill=(0, 0, 0, 140))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        d = ImageDraw.Draw(img)
        d.text((15, 8), line1, fill=(0, 255, 0), font=font)
        d.text((15, 34), line2, fill=lc, font=font)
        img.save(str(dst), "JPEG", quality=95)
        return True
    except Exception:
        if not dst.exists():
            shutil.copy2(str(src), str(dst))
        return False


def step_burn(src: FrameSource, exp: Experiment, out_dir: Path,
              fps_override: str = "60", annotation: str = "imu",
              log_cb=None) -> dict:
    """Burn overlay onto JPEG frames. *annotation* = "imu" or "motor".
    Returns {fps_label: burned_count}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    font = _get_font()

    match_path = out_dir / "match.csv"
    if match_path.exists():
        data = _load_match(match_path)
    else:
        data = exp.match(src.frame_epochs(), src.frame_nums())

    log = src.load_log()
    arrival_ns = np.array([r["arrival_qpc_ns"] for r in log])
    frame_nums = src.frame_nums()
    frames_dir = out_dir / "frames"
    counts = {}

    for label, indices_fn in [
        ("60fps", lambda: range(len(frame_nums))),
        ("30fps", lambda: _resample_30fps(arrival_ns)),
    ]:
        if fps_override not in ("both", label.split("f")[0]):
            continue

        kept = list(indices_fn())
        k_nums = frame_nums[kept]
        burn_dir = out_dir / f"burned_{label}"
        burn_dir.mkdir(parents=True, exist_ok=True)
        n_burned = 0

        for new_i, (ki, fn) in enumerate(zip(kept, k_nums)):
            dst = burn_dir / f"frame_{new_i + 1:06d}.jpg"
            if src.is_bin:
                tmp = burn_dir.parent / "_tmp.jpg"
                tmp.write_bytes(src.read_jpeg(int(fn)))
                n_burned += _burn_one(tmp, data.get(int(fn)), dst, font, annotation)
                tmp.unlink(missing_ok=True)
            else:
                sf = frames_dir / f"frame_{int(fn):06d}.jpg" if frames_dir.exists() else \
                     src.dir / "frames" / f"frame_{int(fn):06d}.jpg"
                n_burned += _burn_one(sf, data.get(int(fn)), dst, font, annotation)

        counts[label] = n_burned
        if log_cb:
            log_cb(f"Burn [{annotation}]: {src.name} {label}: {n_burned}/{len(kept)} frames")

    return counts


def step_encode(out_dir: Path, fps_override: str = "60",
                ffmpeg_path: str = "ffmpeg", log_cb=None) -> list[Path]:
    """Encode burned JPEG sequences to MP4.  Returns list of output paths."""
    outputs = []
    for label, nom_fps in [("60fps", 60.0), ("30fps", None)]:
        if fps_override not in ("both", label.split("f")[0]):
            continue

        burn_dir = out_dir / f"burned_{label}"
        if not burn_dir.is_dir():
            if log_cb:
                log_cb(f"Encode: SKIP {label} — {burn_dir} not found")
            continue

        n_frames = len(list(burn_dir.glob("frame_*.jpg")))
        if label == "30fps":
            log_path = out_dir / "frame_log.csv"
            if log_path.exists():
                with open(log_path, encoding="utf-8") as f:
                    clog = list(_csv.DictReader(f))
                arr_ns = np.array([int(r.get("arrival_qpc_ns", 0)) for r in clog])
                kept = _resample_30fps(arr_ns)
                ep = arr_ns.astype(np.float64) / 1e9
                fps_val = len(kept) / (ep[kept[-1]] - ep[kept[0]]) if len(kept) > 1 else 30.0
            else:
                fps_val = 31.5
        else:
            fps_val = nom_fps

        output_mp4 = out_dir / f"imu_{label}.mp4"
        if log_cb:
            log_cb(f"Encode: {label} {n_frames}f @ {fps_val:.3f}fps → {output_mp4.name}")

        args_list = [
            ffmpeg_path, "-y", "-framerate", f"{fps_val:.6f}",
            "-start_number", "1",
            "-i", str(burn_dir.resolve() / "frame_%06d.jpg"),
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", str(output_mp4.resolve()),
        ]
        r = subprocess.run(args_list, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        if r.returncode == 0:
            outputs.append(output_mp4)
            if log_cb:
                log_cb(f"  Done: {output_mp4.stat().st_size / 1e9:.1f} GB")
        else:
            if log_cb:
                log_cb(f"  FAILED: {r.stderr[-400:]}")
    return outputs
