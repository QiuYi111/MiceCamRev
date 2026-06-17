"""
Create an MSI installer for MiceCam.

Bundles:
  - MiceCam.exe           (PyInstaller single-file, contains ffmpeg + helper)
  - mf_single_frame_helper.exe  (standalone helper for dev/repair)
  - config/               (example configuration files)
  - README.txt            (quick start)

Output: dist/MiceCam_Installer.msi

Usage:
    uv run python scripts/create_msi.py
"""

import msilib
import os
import sys
import shutil
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DIST = PROJECT / "dist"
HELPER = PROJECT / "helpers" / "build" / "Release" / "mf_single_frame_helper.exe"
OUTPUT_MSI = DIST / "MiceCam_Installer.msi"
STAGING = DIST / "msi_staging"

# Product info
PRODUCT_NAME = "MiceCam"
MANUFACTURER = "MiceCam"
UPGRADE_CODE = "{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}"
PRODUCT_VERSION = "0.1.0"

# ── Config files to bundle ──────────────────────────────────────────────
# Create a minimal config if none exists
CONFIG_FILES: dict[str, str] = {
    "config.yaml": """# MiceCam Configuration
# Dual-camera video recording system

# Camera settings
cameras:
  - name: "Camera 1"
    # DirectShow device index (0 = first camera of that name)
    device_number: 0
  - name: "Camera 2"
    device_number: 1

# Recording defaults
recording:
  resolution: "1280x720"
  fps: 60
  codec: "mjpeg"         # mjpeg (passthrough), h264, hevc
  backend: "single_frame_mode"  # single_frame_mode or ffmpeg_video
  output_dir: "./output"
  ring_buffer_capacity: 256

# Timestamp settings
timestamps:
  time_source: "qpc"     # qpc (arrival) or pts (media foundation)
  srt_output: true       # generate SRT subtitle timestamps

# Hardware encoding (ffmpeg mode only)
encoding:
  prefer_hardware: true
  fallback: "libx264"
""",
}


def build_staging() -> Path:
    """Copy all files into the staging directory."""
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)

    # MiceCam.exe
    exe_src = DIST / "MiceCam.exe"
    if not exe_src.exists():
        raise FileNotFoundError(f"MiceCam.exe not found at {exe_src}. Run PyInstaller first.")
    shutil.copy2(exe_src, STAGING / "MiceCam.exe")
    print(f"  + MiceCam.exe ({exe_src.stat().st_size / (1024**2):.1f} MB)")

    # mf_single_frame_helper.exe (standalone)
    if HELPER.exists():
        shutil.copy2(HELPER, STAGING / "mf_single_frame_helper.exe")
        print(f"  + mf_single_frame_helper.exe ({HELPER.stat().st_size} bytes)")

    # Config files
    config_dir = STAGING / "config"
    config_dir.mkdir(exist_ok=True)
    for filename, content in CONFIG_FILES.items():
        (config_dir / filename).write_text(content, encoding="utf-8")
        print(f"  + config/{filename}")

    # README
    readme = STAGING / "README.txt"
    readme.write_text(f"""MiceCam v{PRODUCT_VERSION}
{'=' * 40}

Dual-camera video recording with precise QPC timestamps.

Quick Start
-----------
1. Connect your USB cameras
2. Run MiceCam.exe
3. Select cameras from the dropdown menus
4. Configure resolution (720p), FPS (60), backend (Single Frame Mode)
5. Click "Start Both (Synced)" to begin dual-camera recording
6. Output is saved to ./output/<camera_name>/YYYY-MM-DD/

Files
-----
  MiceCam.exe                  Main application (self-contained)
  mf_single_frame_helper.exe   Media Foundation capture helper
  config/config.yaml           Default configuration

Backends
--------
  Single Frame Mode  Uses Media Foundation to capture individual JPEG frames
                     with QPC arrival timestamps.  Produces frame_log.csv
                     and per-frame JPEG images.  Windows-only.

  FFmpeg Video       Uses ffmpeg to encode MP4 video with PTS timestamps.
                     Supports MJPEG passthrough and H.264 encoding.
                     Cross-platform.

Timestamps
----------
  Both backends use a shared wall_start/steady_start time base for
  cross-camera soft sync.  SRT subtitle files are generated with
  nanosecond-precision UTC timestamps for each frame.

  Single-frame mode records:
    - arrival_qpc_ns:  QPC captured immediately after frame grab
    - mf_pts_100ns:    Media Foundation sample timestamp

  FFmpeg mode records:
    - pkt_pts_time:    Demuxer PTS (normalized to stream start)

Support
-------
  Project: {PROJECT}
""", encoding="utf-8")
    print("  + README.txt")

    return STAGING


def create_msi(staging: Path) -> Path:
    """Build the MSI database using Python's msilib."""

    if OUTPUT_MSI.exists():
        OUTPUT_MSI.unlink()

    # Collect all files to install
    files: list[tuple[str, str, Path]] = []  # (dir, filename, source_path)
    for root, dirs, filenames in os.walk(staging):
        rel_root = Path(root).relative_to(staging)
        for fname in filenames:
            src = Path(root) / fname
            if str(rel_root) == ".":
                files.append(("INSTALLDIR", fname, src))
            else:
                # Subdirectory: use a named directory entry
                subdir_id = f"dir_{str(rel_root).replace(chr(92), '_').replace('/', '_')}"
                files.append((subdir_id, fname, src))

    # Open MSI database
    db = msilib.init_database(
        str(OUTPUT_MSI),
        msilib.schema,
        PRODUCT_NAME,
        UPGRADE_CODE,
        PRODUCT_VERSION,
        MANUFACTURER,
    )

    # ── Feature / Component tables ────────────────────────────────────
    # We use a simple single-feature layout
    feature = msilib.Feature(
        db, "Complete", "MiceCam", "All MiceCam files", 1, 1, "",
    )
    root_feature = msilib.Feature(
        db, "MiceCam", "MiceCam", "MiceCam Application", 0, 1, "INSTALLDIR",
    )
    root_feature.set_current()

    # Directory table
    inst_dir = msilib.Directory(db, "INSTALLDIR", "ProgramFiles64Folder", "MiceCam")
    inst_dir.start_component("MiceCamExe", feature)
    inst_dir.start_component("MiceCamHelper", feature)
    inst_dir.start_component("MiceCamConfig", feature)
    inst_dir.add_file(str(staging / "MiceCam.exe"))
    if HELPER.exists():
        inst_dir.add_file(str(staging / "mf_single_frame_helper.exe"))

    # Config subdirectory
    config_staging = staging / "config"
    if config_staging.exists():
        config_dir = msilib.Directory(db, "ConfigDir", "INSTALLDIR", "config")
        for cf in config_staging.iterdir():
            if cf.is_file():
                config_dir.add_file(str(cf))

    # Shortcut
    shortcut_table = db._ensure_table("Shortcut")
    shortcut_table = db._ensure_table("FeatureComponents")

    # ── Properties ──────────────────────────────────────────────────────
    prop = db._ensure_table("Property")
    prop_cols = [c.name for c in prop._columns]
    # ARPNOMODIFY, ARPNOREMOVE, ARPNOREPAIR
    for key, val in [
        ("ARPCOMMENTS", "Dual-camera video recording with precise QPC timestamps"),
        ("ARPCONTACT", MANUFACTURER),
        ("ARPHELPLINK", "https://github.com/MiceCam"),
        ("ARPURLINFOABOUT", "https://github.com/MiceCam"),
        ("ProductName", PRODUCT_NAME),
        ("Manufacturer", MANUFACTURER),
        ("ALLUSERS", "1"),
    ]:
        prop.AddRow([key, val])

    # ── Shortcut ────────────────────────────────────────────────────────
    try:
        shortcut = msilib.Directory(db, "ProgramMenuFolder", "TARGETDIR", ".")
        shortcut.start_component("MiceCamShortcut", root_feature)
        shortcut.add_file(
            str(staging / "MiceCam.exe"),
            Shortcut={"Name": PRODUCT_NAME},
        )
    except Exception as exc:
        print(f"  [warn] Shortcut creation skipped: {exc}")

    # ── Launch condition / InstallUISequence ─────────────────────────────
    try:
        msilib.add_data(db, "LaunchCondition", [("VersionNT >= 600", "MiceCam requires Windows 10 or later.")])
    except Exception:
        pass

    # ── UI ──────────────────────────────────────────────────────────────
    try:
        msilib.add_data(db, "Error", [
            (0, "MiceCam Setup", "{{FatalError}}", "", "", "", "", "", "", ""),
        ])
        msilib.add_data(db, "UIText", [
            ("FatalError", "MiceCam installation failed."),
        ])
    except Exception:
        pass

    db.Commit()
    return OUTPUT_MSI


def create_msi_simple(staging: Path) -> Path:
    """Alternative approach: use msilib's higher-level API."""
    if OUTPUT_MSI.exists():
        OUTPUT_MSI.unlink()

    # Build the MSI
    db = msilib.init_database(
        str(OUTPUT_MSI),
        msilib.schema,
        PRODUCT_NAME,
        UPGRADE_CODE,
        PRODUCT_VERSION,
        MANUFACTURER,
    )

    feature = msilib.Feature(
        db, "Complete", "MiceCam", "Complete MiceCam installation", 1, 1, None,
    )

    # Root install directory
    root_dir = msilib.Directory(db, "TARGETDIR", "SourceDir")
    pf_dir = msilib.Directory(db, "ProgramFiles64Folder", "TARGETDIR", ".")
    inst_dir = msilib.Directory(db, "INSTALLDIR", "ProgramFiles64Folder", "MiceCam")

    # ── Add files ──────────────────────────────────────────────────────
    def add_file_with_component(dir_obj, filepath: Path, filename: str, feat, component_name: str):
        comp = msilib.Component(db, component_name, "INSTALLDIR", None, None, None, None)
        comp.add_file(str(filepath / filename))
        feat.add_component(comp)

    inst_dir.start_component("MiceCamExe", feature)
    inst_dir.add_file(str(staging / "MiceCam.exe"))

    if HELPER.exists():
        inst_dir.start_component("MiceCamHelper", feature)
        inst_dir.add_file(str(staging / "mf_single_frame_helper.exe"))

    # Config subdirectory
    config_staging = staging / "config"
    if config_staging.exists():
        config_dir = msilib.Directory(db, "ConfigDir", "INSTALLDIR", "config")
        for cf in sorted(config_staging.iterdir()):
            if cf.is_file():
                config_dir.start_component(f"Config_{cf.name.replace('.', '_')}", feature)
                config_dir.add_file(str(cf))

    # ── Properties ──────────────────────────────────────────────────────
    msilib.add_data(db, "Property", [
        ("ALLUSERS", "1"),
        ("ARPCOMMENTS", "Dual-camera video recording with precise QPC timestamps"),
        ("ARPCONTACT", MANUFACTURER),
        ("ARPHELPLINK", "https://github.com/MiceCam"),
        ("ProductName", PRODUCT_NAME),
        ("Manufacturer", MANUFACTURER),
    ])

    # ── Shortcut in Start Menu ──────────────────────────────────────────
    try:
        sm_dir = msilib.Directory(db, "ProgramMenuFolder", "TARGETDIR", ".")
        sm_micecam = msilib.Directory(db, "StartMenuMiceCam", "ProgramMenuFolder", "MiceCam")
        sm_micecam.start_component("MiceCamShortcut", feature)
        sm_micecam.add_file(
            str(staging / "MiceCam.exe"),
            Shortcut={"Name": "MiceCam"},
        )
    except Exception as exc:
        print(f"  [warn] Shortcut: {exc}")

    # ── Registry for Add/Remove Programs ─────────────────────────────────
    msilib.add_data(db, "Registry", [
        ("MiceCamReg", -1, r"Software\MiceCam", "", "MiceCam", ""),
    ])
    msilib.add_data(db, "RegLocator", [
        ("MiceCamReg", 2, r"Software\MiceCam", "", 1),
    ])

    db.Commit()
    return OUTPUT_MSI


def main() -> int:
    print("MiceCam MSI Builder")
    print("=" * 50)

    # Verify inputs
    if not (DIST / "MiceCam.exe").exists():
        print("ERROR: MiceCam.exe not found. Run PyInstaller first:")
        print("  uv run pyinstaller --clean micecam.spec")
        return 1

    # Build staging
    print("\n[1/3] Preparing staging directory...")
    staging = build_staging()

    # Create MSI
    print(f"\n[2/3] Building MSI → {OUTPUT_MSI} ...")
    try:
        msi_path = create_msi_simple(staging)
    except Exception as exc:
        print(f"ERROR: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    size_mb = msi_path.stat().st_size / (1024 * 1024)
    print(f"\n[3/3] Done!")
    print(f"  MSI: {msi_path}")
    print(f"  Size: {size_mb:.1f} MB")

    # Cleanup staging
    shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
