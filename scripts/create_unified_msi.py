"""
Create a unified MSI installer for the MotorDrive + MiceCam system.

Bundles:
  - MotorDrive.exe       (motor control, PID, experiment runner)
  - MiceCam.exe          (dual-camera recording, self-contained)
  - mf_single_frame_helper.exe  (standalone for repair)
  - config/              (example experiment + camera configs)
  - docs/                (IMU manual)

Output: dist/MotorDrive_MiceCam_Setup.msi

Usage:
    uv run python scripts/create_unified_msi.py
"""

import msilib
import os
import shutil
import sys
from pathlib import Path

MICE_DIR = Path(__file__).resolve().parents[1]
MOTOR_DIR = Path("c:/Users/Admin/Documents/MotorDrive")
STAGING = MICE_DIR / "dist" / "unified_staging"
OUTPUT_MSI = MICE_DIR / "dist" / "MotorDrive_MiceCam_Setup.msi"

PRODUCT_NAME = "MotorDrive+MiceCam"
MANUFACTURER = "MiceCam"
UPGRADE_CODE = "{D4E5F6A7-B8C9-01DE-F234-567890ABCDEF}"
PRODUCT_VERSION = "1.0.0"

# ── Source files ───────────────────────────────────────────────────────
MOTORDRIVE_EXE = MOTOR_DIR / "dist" / "MotorDrive.exe"
MICE_EXE       = MICE_DIR / "dist" / "MiceCam.exe"
HELPER_EXE     = MICE_DIR / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe"
MOTOR_CONFIG   = MOTOR_DIR / "config" / "example_experiment.yaml"
IMU_PDF        = MOTOR_DIR / "IMU" / "IM948系列模块使用说明.pdf"


def build_staging():
    """Assemble all files into the staging directory."""
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)

    print("[1/4] Copying executables...")

    # MotorDrive.exe
    if not MOTORDRIVE_EXE.exists():
        raise FileNotFoundError(f"Not found: {MOTORDRIVE_EXE}")
    shutil.copy2(MOTORDRIVE_EXE, STAGING / "MotorDrive.exe")
    sz = MOTORDRIVE_EXE.stat().st_size / (1024 ** 2)
    print(f"  MotorDrive.exe  {sz:.1f} MB")

    # MiceCam.exe
    if not MICE_EXE.exists():
        raise FileNotFoundError(f"Not found: {MICE_EXE}")
    shutil.copy2(MICE_EXE, STAGING / "MiceCam.exe")
    sz = MICE_EXE.stat().st_size / (1024 ** 2)
    print(f"  MiceCam.exe     {sz:.1f} MB")

    # Standalone helper (dev/repair)
    if HELPER_EXE.exists():
        shutil.copy2(HELPER_EXE, STAGING / "mf_single_frame_helper.exe")
        print(f"  mf_single_frame_helper.exe  {HELPER_EXE.stat().st_size} B")

    # ── Config ─────────────────────────────────────────────────────────
    print("\n[2/4] Copying config files...")
    cfg_dir = STAGING / "config"
    cfg_dir.mkdir(exist_ok=True)

    # Motor experiment config
    if MOTOR_CONFIG.exists():
        shutil.copy2(MOTOR_CONFIG, cfg_dir / "example_experiment.yaml")
        print("  config/example_experiment.yaml")

    # MiceCam default config
    (cfg_dir / "micecam_default.yaml").write_text("""# MiceCam — Dual Camera Recorder Configuration
#
# Copy this file and adjust for your setup.

# ── Cameras ──────────────────────────────────────────────────────────
cameras:
  - name: "LRCP  V1080P-60fps"
    device_number: 0
  - name: "LRCP  V1080P-60fps"
    device_number: 1

# ── Recording defaults ───────────────────────────────────────────────
recording:
  resolution: "1280x720"
  fps: 60
  codec: "mjpeg"                  # mjpeg (passthrough), h264, hevc
  backend: "single_frame_mode"    # single_frame_mode or ffmpeg_video
  output_dir: "./output"

# ── Single-frame mode ────────────────────────────────────────────────
single_frame:
  pixel_format: "mjpg"           # mjpg (JPEG), yuy2, nv12, rgb24
  ring_buffer_capacity: 512      # frames in ring buffer

# ── Timestamps ───────────────────────────────────────────────────────
timestamps:
  srt_output: true               # generate SRT subtitle sidecar
  primary_source: "qpc"          # qpc = arrival, pts = MF timestamp
""", encoding="utf-8")
    print("  config/micecam_default.yaml")

    # ── Docs ─────────────────────────────────────────────────────────────
    print("\n[3/4] Copying docs...")
    docs_dir = STAGING / "docs"
    docs_dir.mkdir(exist_ok=True)

    if IMU_PDF.exists():
        shutil.copy2(IMU_PDF, docs_dir / "IM948_IMU_Manual.pdf")
        print(f"  docs/IM948_IMU_Manual.pdf  {IMU_PDF.stat().st_size / 1024:.0f} KB")

    # Quick-start README
    (STAGING / "README.txt").write_text(f"""MotorDrive + MiceCam v{PRODUCT_VERSION}
=========================================

Unified PC software for stepper motor characterization with
dual-camera video recording.

Components
----------
  MotorDrive.exe  — RS485 stepper motor control, PID tuning,
                    automated experiments, IMU closed-loop.

  MiceCam.exe     — Dual USB camera recorder with nanosecond
                    QPC timestamps, SRT sidecar output.
                    Supports single-frame (MF/JPEG) and
                    ffmpeg (MP4) backends.

  mf_single_frame_helper.exe
                  — Media Foundation capture helper for
                    single-frame mode (Windows-only).

Quick Start
-----------
1. Connect RS485 motor controller and IMU
2. Connect one or two USB cameras
3. Launch MotorDrive.exe for motor experiments
4. Launch MiceCam.exe for camera recording
5. Both programs share timestamps via wall_start anchor

MotorDrive Usage
----------------
  • GUI mode:      double-click MotorDrive.exe
  • CLI mode:      MotorDrive.exe --cli
  • Experiment:    python -m motor_drive.experiment config/example_experiment.yaml

MiceCam Usage
-------------
  • GUI mode:      double-click MiceCam.exe
  • Single-frame:  select "Single Frame Mode" backend in the dropdown
  • FFmpeg mode:   select "FFmpeg video" for MP4 output
  • Sync:          click "Start Both (Synced)" for dual-camera

Config Files
------------
  config/example_experiment.yaml  — Motor characterization experiment
  config/micecam_default.yaml     — Camera recording defaults

Output
------
  experiment_data/   — Motor trial JSON + serial logs
  output/            — Camera recordings (JPEG/MP4 + frame_log.csv)

Support
-------
  Project: {MICE_DIR}
""", encoding="utf-8")
    print("  README.txt")

    # ── Total size ──────────────────────────────────────────────────────
    total = sum(
        sum(f.stat().st_size for f in (STAGING / d).rglob("*") if f.is_file())
        for d in [""] if (STAGING / d).is_dir()
    )
    # Simpler:
    total = 0
    for f in STAGING.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    print(f"\n[4/4] Staging total: {total / (1024**2):.1f} MB")
    return STAGING


def create_msi(staging: Path) -> Path:
    """Build the MSI database."""
    if OUTPUT_MSI.exists():
        OUTPUT_MSI.unlink()

    db = msilib.init_database(
        str(OUTPUT_MSI),
        msilib.schema,
        PRODUCT_NAME,
        UPGRADE_CODE,
        PRODUCT_VERSION,
        MANUFACTURER,
    )

    feature = msilib.Feature(
        db, "Complete", PRODUCT_NAME,
        "MotorDrive + MiceCam unified installation", 1, 1, None,
    )

    # ── Directory tree ──────────────────────────────────────────────────
    root = msilib.Directory(db, "TARGETDIR", "SourceDir")
    msilib.Directory(db, "ProgramFiles64Folder", "TARGETDIR", ".")
    inst_dir = msilib.Directory(db, "INSTALLDIR", "ProgramFiles64Folder", "MotorDrive_MiceCam")

    # Top-level executables
    for fname in ["MotorDrive.exe", "MiceCam.exe", "mf_single_frame_helper.exe", "README.txt"]:
        src = staging / fname
        if src.exists():
            inst_dir.start_component(f"comp_{fname.replace('.', '_')}", feature)
            inst_dir.add_file(str(src))

    # Config subdirectory
    cfg_staging = staging / "config"
    if cfg_staging.exists():
        cfg_dir = msilib.Directory(db, "ConfigDir", "INSTALLDIR", "config")
        for cf in sorted(cfg_staging.iterdir()):
            if cf.is_file():
                comp_name = f"comp_config_{cf.name.replace('.', '_')}"
                cfg_dir.start_component(comp_name, feature)
                cfg_dir.add_file(str(cf))

    # Docs subdirectory
    docs_staging = staging / "docs"
    if docs_staging.exists():
        docs_msi = msilib.Directory(db, "DocsDir", "INSTALLDIR", "docs")
        for df in sorted(docs_staging.iterdir()):
            if df.is_file():
                comp_name = f"comp_docs_{df.name.replace('.', '_')[:50]}"
                docs_msi.start_component(comp_name, feature)
                docs_msi.add_file(str(df))

    # ── Start Menu shortcuts ────────────────────────────────────────────
    try:
        sm = msilib.Directory(db, "ProgramMenuFolder", "TARGETDIR", ".")
        sm_dir = msilib.Directory(db, "StartMenuDir", "ProgramMenuFolder", "MotorDrive+MiceCam")

        sm_dir.start_component("comp_shortcut_motor", feature)
        sm_dir.add_file(str(staging / "MotorDrive.exe"), Shortcut={"Name": "MotorDrive"})

        sm_dir.start_component("comp_shortcut_mice", feature)
        sm_dir.add_file(str(staging / "MiceCam.exe"), Shortcut={"Name": "MiceCam"})
    except Exception as exc:
        print(f"  [warn] Shortcuts: {exc}")

    # ── Properties ──────────────────────────────────────────────────────
    msilib.add_data(db, "Property", [
        ("ALLUSERS", "1"),
        ("ARPCOMMENTS", "MotorDrive stepper motor control + MiceCam dual-camera recording"),
        ("ARPCONTACT", MANUFACTURER),
        ("ProductName", PRODUCT_NAME),
        ("Manufacturer", MANUFACTURER),
        ("ARPURLINFOABOUT", "https://github.com/MiceCam"),
    ])

    # ── Launch condition ────────────────────────────────────────────────
    try:
        msilib.add_data(db, "LaunchCondition",
                        [("VersionNT >= 600",
                          "Requires Windows 10 or later.")])
    except Exception:
        pass

    db.Commit()
    return OUTPUT_MSI


def main() -> int:
    print("=" * 55)
    print("  MotorDrive + MiceCam — Unified MSI Builder")
    print("=" * 55)

    staging = build_staging()
    print(f"\nBuilding MSI → {OUTPUT_MSI} ...")

    try:
        msi = create_msi(staging)
    except Exception as exc:
        print(f"ERROR: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    size_mb = msi.stat().st_size / (1024 ** 2)
    print(f"\nDone!")
    print(f"  MSI:  {msi}")
    print(f"  Size: {size_mb:.1f} MB")

    shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
