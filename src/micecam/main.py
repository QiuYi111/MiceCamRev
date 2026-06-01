"""Entry point module for the ``micecam`` console script.

Supports ``--check`` flag for smoke-testing in CI without launching the GUI.
"""

import logging
import sys


def _run_smoke_test() -> int:
    """Verify all modules import and critical objects are functional."""
    import tempfile
    from pathlib import Path

    errors: list[str] = []

    def check(desc: str, ok: bool, detail: str = "") -> None:
        status = "OK" if ok else "FAIL"
        line = f"  [{status}] {desc}"
        if not ok:
            line += f"  -- {detail}"
            errors.append(f"{desc}: {detail}")
        print(line)

    print("MiceCam Smoke Test")
    print("==================")

    # 1. Core imports
    print("\n1. Core modules")
    try:
        from micecam.camera_manager import CameraInfo, list_cameras, get_preferred_encoder
        check("camera_manager", True)
    except Exception as e:
        check("camera_manager", False, str(e))

    try:
        from micecam.recorder import Recorder
        check("recorder", True)
    except Exception as e:
        check("recorder", False, str(e))

    try:
        from micecam.timestamp import TimestampWriter
        check("timestamp", True)
    except Exception as e:
        check("timestamp", False, str(e))

    try:
        from micecam.core.sync_controller import SyncController
        check("sync_controller", True)
    except Exception as e:
        check("sync_controller", False, str(e))

    try:
        from micecam.utils.platform import is_macos, is_windows, ffmpeg_device_format
        check("utils.platform", True)
    except Exception as e:
        check("utils.platform", False, str(e))

    try:
        from micecam.utils.resource_path import get_ffmpeg_path
        check("utils.resource_path", True)
    except Exception as e:
        check("utils.resource_path", False, str(e))

    try:
        from micecam.services.disk_monitor import DiskMonitor
        check("services.disk_monitor", True)
    except Exception as e:
        check("services.disk_monitor", False, str(e))

    # 2. TimestampWriter shared-refs (soft sync)
    print("\n2. Soft-sync timestamp logic")
    d = tempfile.mkdtemp()
    try:
        tw = TimestampWriter(Path(d) / "test.srt")
        tw.start(wall_start=1717171200.0, steady_start=1000000000000)
        assert tw.wall_start == 1717171200.0
        assert tw.steady_start == 1000000000000
        check("TimestampWriter external refs", True)
    except Exception as e:
        check("TimestampWriter external refs", False, str(e))

    # 3. Recorder
    print("\n3. Recorder")
    try:
        r = Recorder("0", "Test", Path(d))
        assert r.output_path is None
        assert r.srt_path is None
        assert r.is_recording() is False
        check("Recorder properties", True)
    except Exception as e:
        check("Recorder properties", False, str(e))

    # 4. SyncController
    print("\n4. SyncController")
    try:
        sc = SyncController()
        assert sc.is_recording is False
        assert sc.active_count == 0
        check("SyncController init", True)
    except Exception as e:
        check("SyncController init", False, str(e))

    # 5. Camera enumeration (without probing -- fast path)
    print("\n5. Camera enumeration (fast, no probing)")
    try:
        cameras = list_cameras(probe_capabilities=False)
        check(f"list_cameras -> {len(cameras)} found", len(cameras) >= 0)
        for cam in cameras:
            native = f"  native={cam.native_codec}" if cam.native_codec else ""
            print(f"     [{cam.index}] {cam.name}{native}")
    except Exception as e:
        check("list_cameras", False, str(e))

    # 6. Encoder detection
    print("\n6. Encoder detection")
    try:
        from micecam.camera_manager import get_available_encoders
        encoders = get_available_encoders()
        check(f"encoders found: {len(encoders)}", len(encoders) >= 0)
        for enc in encoders:
            hw = "HW" if enc.hardware_accelerated else "SW"
            print(f"     {enc.name} ({hw}, {enc.codec})")
    except Exception as e:
        check("encoders", False, str(e))

    # 7. FFmpeg path
    print("\n7. FFmpeg binary")
    try:
        ffmpeg = get_ffmpeg_path()
        check(f"ffmpeg path: {ffmpeg}", bool(ffmpeg))
    except Exception as e:
        check("ffmpeg path", False, str(e))

    # 8. GUI imports (no window creation -- just verify PyQt works)
    print("\n8. GUI imports")
    try:
        from PyQt6 import QtWidgets, QtCore, QtGui
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication(sys.argv)
        check("PyQt6 import", True)
        check("QApplication created", True)
        app.quit()
    except Exception as e:
        check("PyQt6", False, str(e))

    import shutil
    shutil.rmtree(d, ignore_errors=True)

    print(f"\n{'='*30}")
    if errors:
        print(f"FAILED -- {len(errors)} check(s) failed:")
        for e in errors:
            print(f"  FAIL: {e}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def main() -> None:
    """Entry point for the ``micecam`` console script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if "--check" in sys.argv:
        code = _run_smoke_test()
        sys.exit(code)

    from PyQt6 import QtWidgets

    from micecam.gui.main_window import MainWindow

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MiceCam")
    app.setOrganizationName("MiceCam")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
