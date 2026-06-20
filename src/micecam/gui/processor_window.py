"""
motorvids pipeline GUI — decode → analyze → align → burn → encode.

Launched from MiceCam's Tools menu.  Operates on a unified experiment
directory containing camera recordings + MotorDrive experiment JSON.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6 import QtCore, QtGui, QtWidgets

from micecam.processor import (
    FrameSource, Experiment, find_sources,
    step_decode, step_analyze, step_align, step_burn, step_encode,
)
from micecam.camera_manager import get_ffmpeg_path

logger = logging.getLogger(__name__)


class PipelineWorker(QtCore.QThread):
    """Runs the pipeline steps sequentially in a background thread."""

    log_line = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(int)     # 0-100
    step_done = QtCore.pyqtSignal(str)   # step name
    finished = QtCore.pyqtSignal(bool)    # success
    ask_stop = QtCore.pyqtSignal()        # emitted if user cancelled

    def __init__(self, experiment_dir: Path, steps: dict, fps: str,
                 annotation: str = "imu",
                 parent: Optional[QtCore.QObject] = None):
        super().__init__(parent)
        self.experiment_dir = Path(experiment_dir)
        self.steps = steps            # {"decode": bool, ...}
        self.fps = fps                # "60", "30", "both"
        self.annotation = annotation  # "imu" or "motor"
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _log(self, msg: str) -> None:
        self.log_line.emit(msg)

    def run(self) -> None:
        try:
            root = self.experiment_dir
            cameras_dir = root / "cameras"
            analysis_dir = root / "analysis"
            aligned_dir = root / "aligned"

            root.mkdir(parents=True, exist_ok=True)

            # ── Discover sources ─────────────────────────────────
            recorder_root = root / "cameras" if cameras_dir.exists() else root
            sources = find_sources(recorder_root)
            if not sources:
                sources = find_sources(root)  # fallback: search entire tree
            if not sources:
                self._log("ERROR: No camera recordings found.")
                self.finished.emit(False)
                return

            self._log(f"Found {len(sources)} camera source(s):")
            for s in sources:
                self._log(f"  {s.name}  ({s.n_frames} frames, "
                          f"{'bin' if s.is_bin else 'single'})")

            # ── Load experiment ──────────────────────────────────
            exp = None
            exp_json = root / "experiment.json"
            if not exp_json.exists():
                exp_json = root  # may be a dir with mice_*.json
            try:
                exp = Experiment(exp_json)
                self._log(f"Experiment: {exp.name} ({exp.n_trials} trials)")
            except FileNotFoundError:
                self._log("WARNING: No experiment.json found — align/burn will be skipped")

            total_steps = sum(1 for v in self.steps.values() if v)
            step_num = 0

            for src in sources:
                if self._cancelled:
                    self._log("Pipeline cancelled.")
                    break

                cam_dir = root / "cameras" / src.name if cameras_dir.exists() else analysis_dir / src.name
                ana_dir = analysis_dir / src.name
                aln_dir = aligned_dir / src.name
                vid_dir = root / "videos" / src.name

                # ── Decode ──
                if self.steps.get("decode"):
                    if self._cancelled: break
                    step_num += 1
                    self.progress.emit(int(step_num / total_steps * 100))
                    self.step_done.emit("decode")
                    step_decode(src, cam_dir, log_cb=self._log)

                # ── Analyze ──
                if self.steps.get("analyze"):
                    if self._cancelled: break
                    step_num += 1
                    self.progress.emit(int(step_num / total_steps * 100))
                    self.step_done.emit("analyze")
                    step_analyze(src, ana_dir, log_cb=self._log)

                # ── Align ──
                if self.steps.get("align") and exp is not None:
                    if self._cancelled: break
                    step_num += 1
                    self.progress.emit(int(step_num / total_steps * 100))
                    self.step_done.emit("align")
                    step_align(src, exp, aln_dir, log_cb=self._log)

                # ── Burn ──
                if self.steps.get("burn") and exp is not None:
                    if self._cancelled: break
                    step_num += 1
                    self.progress.emit(int(step_num / total_steps * 100))
                    self.step_done.emit("burn")
                    step_burn(src, exp, cam_dir, fps_override=self.fps,
                              annotation=self.annotation, log_cb=self._log)

                # ── Encode ──
                if self.steps.get("encode"):
                    if self._cancelled: break
                    step_num += 1
                    self.progress.emit(int(step_num / total_steps * 100))
                    self.step_done.emit("encode")
                    vid_dir.mkdir(parents=True, exist_ok=True)
                    # encode from the cam_dir (where burned_* subdirs are)
                    step_encode(cam_dir, fps_override=self.fps,
                                ffmpeg_path=get_ffmpeg_path(), log_cb=self._log)

            self.progress.emit(100)
            self._log("Pipeline complete.")
            self.finished.emit(True)

        except Exception as exc:
            self._log(f"FATAL: {exc}")
            logger.exception("Pipeline failed")
            self.finished.emit(False)


class ProcessorWindow(QtWidgets.QDialog):
    """Modal dialog wrapping the motorvids pipeline."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Experiment Video Processor")
        self.setMinimumSize(700, 500)
        self._worker: Optional[PipelineWorker] = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Experiment directory ─────────────────────────────────
        dir_row = QtWidgets.QHBoxLayout()
        dir_row.addWidget(QtWidgets.QLabel("Experiment dir:"))
        self._dir_edit = QtWidgets.QLineEdit(str(Path.cwd()))
        self._dir_edit.setToolTip("Unified experiment directory "
            "(contains experiment.json + cameras/ subdirs).")
        dir_row.addWidget(self._dir_edit, 1)
        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(browse_btn)
        scan_btn = QtWidgets.QPushButton("Scan")
        scan_btn.setToolTip("Auto-detect camera recordings under this directory.")
        scan_btn.clicked.connect(self._scan)
        dir_row.addWidget(scan_btn)
        layout.addLayout(dir_row)

        # ── Scan results ─────────────────────────────────────────
        self._scan_label = QtWidgets.QLabel("No sources found — click Scan")
        self._scan_label.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._scan_label)

        # ── Pipeline steps ───────────────────────────────────────
        steps_group = QtWidgets.QGroupBox("Pipeline Steps")
        steps_layout = QtWidgets.QVBoxLayout(steps_group)

        self._step_decode = QtWidgets.QCheckBox("Decode  —  extract JPEG frames from recording")
        self._step_decode.setChecked(True)
        steps_layout.addWidget(self._step_decode)

        self._step_analyze = QtWidgets.QCheckBox("Analyze  —  frame interval stats + plots")
        self._step_analyze.setChecked(True)
        steps_layout.addWidget(self._step_analyze)

        self._step_align = QtWidgets.QCheckBox("Align  —  match IMU data to camera frames")
        self._step_align.setChecked(True)
        steps_layout.addWidget(self._step_align)

        self._step_burn = QtWidgets.QCheckBox("Burn  —  draw IMU overlay on frames")
        self._step_burn.setChecked(True)
        steps_layout.addWidget(self._step_burn)

        self._step_encode = QtWidgets.QCheckBox("Encode  —  ffmpeg JPEG→MP4")
        self._step_encode.setChecked(True)
        steps_layout.addWidget(self._step_encode)

        # FPS selector
        fps_row = QtWidgets.QHBoxLayout()
        fps_row.addWidget(QtWidgets.QLabel("FPS mode:"))
        self._fps_combo = QtWidgets.QComboBox()
        self._fps_combo.addItem("60 fps only", "60")
        self._fps_combo.addItem("30 fps only", "30")
        self._fps_combo.addItem("Both (60 + 30)", "both")
        self._fps_combo.setCurrentIndex(2)  # both by default
        fps_row.addWidget(self._fps_combo)
        fps_row.addStretch()
        steps_layout.addLayout(fps_row)

        # Annotation source
        anno_row = QtWidgets.QHBoxLayout()
        anno_row.addWidget(QtWidgets.QLabel("Annotation:"))
        self._anno_combo = QtWidgets.QComboBox()
        self._anno_combo.addItem("IMU (yaw/gyro)", "imu")
        self._anno_combo.addItem("Motor (speed/angle)", "motor")
        self._anno_combo.setToolTip(
            "IMU: overlay yaw angle, gyro rate.\n"
            "Motor: overlay motor-reported speed, angle, pulses."
        )
        anno_row.addWidget(self._anno_combo)
        anno_row.addStretch()
        steps_layout.addLayout(anno_row)

        # Output structure info
        info_lbl = QtWidgets.QLabel(
            "Output: <experiment_dir>/cameras/ | analysis/ | aligned/ | videos/"
        )
        info_lbl.setStyleSheet("color: #888; font-size: 10px;")
        steps_layout.addWidget(info_lbl)

        layout.addWidget(steps_group)

        # ── Action buttons ───────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()
        self._run_btn = QtWidgets.QPushButton("▶  Run Pipeline")
        self._run_btn.setMinimumHeight(36)
        self._run_btn.setStyleSheet(
            "QPushButton { background: #27ae60; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #2ecc71; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self._run_btn.clicked.connect(self._run_pipeline)
        btn_row.addWidget(self._run_btn)

        self._stop_btn = QtWidgets.QPushButton("■  Stop")
        self._stop_btn.setMinimumHeight(36)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { background: #c0392b; color: white; font-weight: bold; "
            "border-radius: 4px; padding: 6px 16px; }"
            "QPushButton:hover { background: #e74c3c; }"
            "QPushButton:disabled { background: #555; color: #999; }"
        )
        self._stop_btn.clicked.connect(self._stop_pipeline)
        btn_row.addWidget(self._stop_btn)

        layout.addLayout(btn_row)

        # ── Progress bar ─────────────────────────────────────────
        self._progress = QtWidgets.QProgressBar()
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # ── Log ──────────────────────────────────────────────────
        self._log_widget = QtWidgets.QPlainTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setMaximumBlockCount(2000)
        self._log_widget.setFont(QtGui.QFont("Consolas", 9))
        layout.addWidget(self._log_widget, 1)

        # Close button
        close_row = QtWidgets.QHBoxLayout()
        close_row.addStretch()
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    # ── Slots ────────────────────────────────────────────────────

    def _browse_dir(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Experiment Directory", self._dir_edit.text())
        if d:
            self._dir_edit.setText(d)
            self._scan()

    def _scan(self) -> None:
        root = Path(self._dir_edit.text())
        sources = find_sources(root)
        if not sources:
            sources = find_sources(root / "cameras") if (root / "cameras").exists() else []
        if not sources:
            self._scan_label.setText("No camera recordings found.")
            self._scan_label.setStyleSheet("color: #e74c3c; font-size: 11px;")
            return

        lines = []
        for s in sources:
            lines.append(f"{s.name}: {s.n_frames} frames, "
                         f"{'bin' if s.is_bin else 'single'}, "
                         f"{s.nominal_fps} fps")

        exp_json = root / "experiment.json"
        if not exp_json.exists():
            matches = list(root.glob("mice_*.json"))
            if matches:
                exp_json = matches[0]
        if exp_json.exists():
            try:
                exp = Experiment(exp_json)
                lines.append(f"Experiment: {exp.name} ({exp.n_trials} trials)")
            except Exception:
                lines.append("Experiment: (parse error)")

        self._scan_label.setText("\n".join(lines))
        self._scan_label.setStyleSheet("color: #27ae60; font-size: 11px;")

    def _run_pipeline(self) -> None:
        root = Path(self._dir_edit.text())
        steps = {
            "decode": self._step_decode.isChecked(),
            "analyze": self._step_analyze.isChecked(),
            "align": self._step_align.isChecked(),
            "burn": self._step_burn.isChecked(),
            "encode": self._step_encode.isChecked(),
        }
        if not any(steps.values()):
            QtWidgets.QMessageBox.warning(self, "No Steps", "Select at least one pipeline step.")
            return

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._progress.setValue(0)
        self._log_widget.clear()

        self._worker = PipelineWorker(root, steps,
                                       self._fps_combo.currentData(),
                                       self._anno_combo.currentData())
        self._worker.log_line.connect(self._append_log)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.step_done.connect(
            lambda name: self._log_widget.appendPlainText(f"── {name} ──"))
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop_pipeline(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._append_log("(cancelling — will stop after current step)")

    def _on_finished(self, success: bool) -> None:
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        if success:
            self._append_log("All steps completed successfully.")
        else:
            self._append_log("Pipeline terminated with errors.")

    def _append_log(self, msg: str) -> None:
        self._log_widget.appendPlainText(msg)
        # Auto-scroll
        bar = self._log_widget.verticalScrollBar()
        bar.setValue(bar.maximum())
