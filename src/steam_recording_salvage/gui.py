from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

import vlc
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QSizePolicy,
)

from .scan import discover_sessions, find_sessions, RecordingSession
from .exporter import export_session, ExportError


@dataclass
class MediaStats:
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    audio_desc: Optional[str] = None
    container: Optional[str] = None

    def has_audio(self) -> Optional[bool]:
        # Slightly goofy helper, but it keeps the call sites readable:
        # - None means “I genuinely don’t know yet”
        # - True means “audio stream exists”
        if self.acodec is None:
            return None
        return True


def _format_time(ms: int) -> str:
    # Defensive: VLC sometimes gives -1 for time when it’s starting/stopping.
    if ms < 0:
        ms = 0
    total_s = ms // 1000
    m = total_s // 60
    s = total_s % 60
    return f"{m:02d}:{s:02d}"


def _safe_float(x: Any) -> Optional[float]:
    # ffprobe likes strings for numbers sometimes. This keeps us chill about it.
    try:
        return float(x)
    except Exception:
        return None


def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("Steam Recording Salvage")

        # A bit of caching helps with MPD + segments (especially near the end).
        # This is best-effort, and it doesn’t break anything if VLC ignores it.
        # Also: no-video-title-show removes that random “filename” overlay VLC can do.
        self._vlc_instance = vlc.Instance(
            "--no-video-title-show",
            "--file-caching=2000",
            "--network-caching=2000",
        )
        self._vlc_player = self._vlc_instance.media_player_new()

        # Hook VLC events so we know when it REALLY ended (time math isn't reliable here).
        # MPD playback can “look ended” without time==length, so this is the reliable signal.
        self._vlc_events = self._vlc_player.event_manager()
        self._vlc_events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end_reached)
        self._vlc_events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error)

        self._current_session: Optional[RecordingSession] = None
        self._current_stats: MediaStats = MediaStats()

        # Scrub state: while you're dragging the slider, we treat it like “preview mode”
        # and we don’t let the timer yank the slider around.
        self._is_scrubbing = False
        self._pending_seek_ms: Optional[int] = None

        # Total duration cache (ms). VLC can be a little slow to provide it for MPDs.
        self._total_ms: int = 0

        # Tiny cache so we don’t re-run ffprobe if you re-select the same item.
        self._last_stats_key: Optional[str] = None

        # Trim range in milliseconds (export converts to seconds later).
        self._trim_in_ms: Optional[int] = None
        self._trim_out_ms: Optional[int] = None

        # Track whether VLC ended or got stuck at the end.
        # This drives the "Play restarts" and "scrub after end revives" logic.
        self._reached_end = False

        # Used for the "stop -> play -> seek" revive sequence.
        # (VLC sometimes needs a kick after EOF before it obeys seeking again.)
        self._resume_after_seek = False
        self._seek_after_restart_ms: Optional[int] = None
        self._restart_seek_attempts: int = 0

        # Scrub behaviour you asked for:
        # Pause while the user drags, then resume from the exact spot they drop.
        self._was_playing_before_scrub = False

        # Guard so the UI doesn't fight the seek while VLC is landing.
        # Without this, _on_tick can overwrite the label/slider while we’re mid-seek.
        self._seeking = False
        self._seek_target_ms: Optional[int] = None
        self._seek_clear_timer: Optional[QTimer] = None

        # Seek settling tries (MPD seeks land on keyframes/segment boundaries sometimes).
        # So we “settle” for a few ticks, then accept whatever time VLC actually landed on.
        self._seek_settle_timer: Optional[QTimer] = None
        self._seek_settle_deadline_ms: int = 0  # not real milliseconds, just a tick counter
        self._seek_settle_resume: bool = False

        self.root_edit = QLineEdit()
        self.browse_root_btn = QPushButton("Browse…")

        self.sessions_list = QListWidget()

        self.video_frame = QFrame()
        self.video_frame.setFrameShape(QFrame.StyledPanel)
        self.video_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_frame.setMinimumHeight(420)  # this is the big change: make preview feel “taller”

        self.play_btn = QPushButton("Play")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.seek = QSlider(Qt.Horizontal)
        self.seek.setRange(0, 10_000)
        self.seek.setSingleStep(10)
        self.seek.setPageStep(100)

        self.time_lbl = QLabel("00:00 / 00:00")

        self.trim_lbl = QLabel("Trim: full")
        self.set_in_btn = QPushButton("Set In")
        self.set_out_btn = QPushButton("Set Out")
        self.clear_trim_btn = QPushButton("Clear")

        self.audio_indicator = QLabel("Audio: unknown")
        self.audio_indicator.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.accurate_trim_chk = QCheckBox("Accurate trim (slower)")
        self.accurate_trim_chk.setChecked(True)

        self.stats_title = QLabel("Video stats")
        self.stats_duration = QLabel("Duration: —")
        self.stats_res = QLabel("Resolution: —")
        self.stats_fps = QLabel("FPS: —")
        self.stats_v = QLabel("Video: —")
        self.stats_a = QLabel("Audio: —")
        self.stats_container = QLabel("Container: —")

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        self.out_edit = QLineEdit()
        self.browse_out_btn = QPushButton("Browse…")
        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)

        self._apply_compact_ui()
        self._build_layout()
        self._wire_ui()

        self._set_defaults()
        self._refresh_sessions()

        # One timer loop to keep UI in sync with VLC.
        # We keep it smooth, but not “spammy”.
        self._tick = QTimer(self)
        self._tick.setInterval(120)  # keep it smooth but not too spammy
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    def _apply_compact_ui(self) -> None:
        # Smaller font so the minimum size can still look clean without overlaps.
        # This is the “make small elements smaller so big elements can breathe” part.
        f = QFont()
        f.setPointSize(12)  # you can tune this 11–12 if you want
        self.setFont(f)

        # Make buttons a bit tighter so they don’t hog vertical space.
        for b in [
            self.play_btn,
            self.stop_btn,
            self.set_in_btn,
            self.set_out_btn,
            self.clear_trim_btn,
            self.export_btn,
            self.browse_out_btn,
            self.browse_root_btn,
        ]:
            b.setMinimumHeight(28)

        # Keep the seekbar slim so the preview gets more love.
        self.seek.setMinimumHeight(20)

    def showEvent(self, event) -> None:
        super().showEvent(event)

        # Lock the minimum size to whatever we open at.
        # So users can’t shrink it into a broken layout.
        self.setMinimumSize(self.size())

    def _build_layout(self) -> None:
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Recordings root"))
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self.browse_root_btn)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Export to"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.browse_out_btn)

        # Left side: Sessions (taller) + stats (near the bottom)
        left_col = QVBoxLayout()
        left_col.addWidget(QLabel("Sessions found"))
        left_col.addWidget(self.sessions_list, 1)

        stats_box = QFrame()
        stats_box.setFrameShape(QFrame.StyledPanel)
        stats_layout = QVBoxLayout()
        stats_layout.addWidget(self.stats_title)
        stats_layout.addWidget(self.stats_duration)
        stats_layout.addWidget(self.stats_res)
        stats_layout.addWidget(self.stats_fps)
        stats_layout.addWidget(self.stats_v)
        stats_layout.addWidget(self.stats_a)
        stats_layout.addWidget(self.stats_container)
        stats_layout.addStretch(1)
        stats_box.setLayout(stats_layout)

        left_col.addWidget(stats_box, 0)

        left_widget = QWidget()
        left_widget.setLayout(left_col)

        # Right side: Preview + controls (below preview) + log
        right_col = QVBoxLayout()
        right_col.addWidget(QLabel("Preview"))
        right_col.addWidget(self.video_frame, 1)

        controls_row = QHBoxLayout()
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.stop_btn)
        controls_row.addWidget(self.seek, 1)
        controls_row.addWidget(self.time_lbl)
        right_col.addLayout(controls_row)

        trim_row = QHBoxLayout()
        trim_row.addWidget(self.trim_lbl)
        trim_row.addStretch(1)
        # Put Set In / Set Out in the middle like you asked
        trim_row.addWidget(self.set_in_btn)
        trim_row.addWidget(self.set_out_btn)
        trim_row.addWidget(self.clear_trim_btn)
        trim_row.addStretch(1)
        trim_row.addWidget(self.audio_indicator)
        trim_row.addWidget(self.accurate_trim_chk)
        right_col.addLayout(trim_row)

        right_col.addWidget(QLabel("Log"))
        right_col.addWidget(self.log, 0)
        self.log.setMinimumHeight(140)

        right_widget = QWidget()
        right_widget.setLayout(right_col)

        # Use a splitter so left vs right stays proportional when resizing.
        mid_split = QSplitter(Qt.Horizontal)
        mid_split.addWidget(left_widget)
        mid_split.addWidget(right_widget)
        mid_split.setStretchFactor(0, 0)
        mid_split.setStretchFactor(1, 1)
        mid_split.setSizes([340, 860])

        main_col = QVBoxLayout()
        main_col.addLayout(root_row)
        main_col.addWidget(mid_split, 1)
        main_col.addLayout(out_row)
        main_col.addWidget(self.export_btn)
        self.setLayout(main_col)

    def _wire_ui(self) -> None:
        self.browse_root_btn.clicked.connect(self._choose_root)
        self.browse_out_btn.clicked.connect(self._choose_out_dir)
        self.root_edit.editingFinished.connect(self._refresh_sessions)
        self.sessions_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.export_btn.clicked.connect(self._export_selected)

        self.play_btn.clicked.connect(self._toggle_play_pause)
        self.stop_btn.clicked.connect(self._stop_preview)

        # Seekbar scrubbing behaviour
        self.seek.sliderPressed.connect(self._on_slider_pressed)
        self.seek.sliderReleased.connect(self._on_slider_released)
        self.seek.valueChanged.connect(self._on_slider_value_changed)

        # Trim controls
        self.set_in_btn.clicked.connect(self._set_trim_in)
        self.set_out_btn.clicked.connect(self._set_trim_out)
        self.clear_trim_btn.clicked.connect(self._clear_trim)

    def _set_defaults(self) -> None:
        sessions = discover_sessions()
        if sessions:
            self.root_edit.setText(str(sessions[0].mpd_path.parents[3]))
        else:
            self.root_edit.setText(str(Path.home()))

        self.out_edit.setText(str(Path.home() / "Desktop"))

        # Open at a sensible size. This becomes the minimum size due to showEvent().
        self.resize(1200, 760)

    def _choose_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select recordings root")
        if folder:
            self.root_edit.setText(folder)
            self._refresh_sessions()

    def _choose_out_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select export folder")
        if folder:
            self.out_edit.setText(folder)

    def _refresh_sessions(self) -> None:
        self.sessions_list.clear()
        self._current_session = None
        self.export_btn.setEnabled(False)
        self.play_btn.setEnabled(False)

        # When switching roots, we’re basically resetting the whole player state.
        self._stop_preview()
        self._clear_trim()
        self._set_stats(MediaStats())

        root = Path(self.root_edit.text()).expanduser()
        sessions = find_sessions(root) if root.exists() else []

        if not sessions:
            self._log(f"No sessions found under: {root}")
            return

        for s in sessions:
            item = QListWidgetItem(s.mpd_path.as_posix())
            item.setData(Qt.UserRole, s)
            self.sessions_list.addItem(item)

        self._log(f"Found {len(sessions)} session(s). Select one to preview or export.")

    def _on_selection_changed(self) -> None:
        item = self.sessions_list.currentItem()
        if not item:
            self._current_session = None
            self.export_btn.setEnabled(False)
            self.play_btn.setEnabled(False)
            return

        self._current_session = item.data(Qt.UserRole)
        self.export_btn.setEnabled(True)
        self.play_btn.setEnabled(True)

        # Reset trim on new selection so you don’t accidentally export the wrong range.
        self._clear_trim()

        # New video = fresh end-state.
        self._reached_end = False

        # Auto start preview to match Steam-like feel.
        self._start_preview()

        # Pull stats as soon as we select something.
        self._read_stats_for_current()

    def _attach_vlc_to_widget(self) -> None:
        # VLC needs the native window handle, and it’s different per OS.
        wid = int(self.video_frame.winId())

        if sys.platform.startswith("win"):
            self._vlc_player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self._vlc_player.set_nsobject(wid)
        else:
            self._vlc_player.set_xwindow(wid)

    def _start_preview(self) -> None:
        if not self._current_session:
            return

        mpd = self._current_session.mpd_path
        if not mpd.exists():
            self._log(f"Missing mpd: {mpd}")
            return

        self._attach_vlc_to_widget()

        # MPD loads through VLC directly (nice because we don’t have to stitch segments manually here).
        media = self._vlc_instance.media_new(str(mpd))
        self._vlc_player.set_media(media)

        ok = self._vlc_player.play()
        if ok == -1:
            self._log("VLC failed to start playback. (libVLC not found / VLC not installed?)")
            return

        self.stop_btn.setEnabled(True)
        self.play_btn.setText("Pause")
        self._reached_end = False
        self._log(f"Previewing: {mpd}")

    def _toggle_play_pause(self) -> None:
        # One button that flips between Play and Pause, like a normal player.
        if self._vlc_player.is_playing():
            self._vlc_player.pause()
            self.play_btn.setText("Play")
            return

        # If nothing is loaded yet, start preview.
        if self._current_session and self._vlc_player.get_media() is None:
            self._start_preview()
            return

        # If VLC ended, Play should behave like a normal player and restart.
        # (This prevents the “Play does nothing because VLC thinks it’s at EOF” vibe.)
        if self._reached_end:
            self._restart_and_seek(0, autoplay=True)
            return

        self._vlc_player.play()
        self.play_btn.setText("Pause")
        self.stop_btn.setEnabled(True)

    def _stop_preview(self) -> None:
        # Stop should fully reset the playback state.
        try:
            self._vlc_player.stop()
        except Exception:
            pass

        self.stop_btn.setEnabled(False)
        self.play_btn.setText("Play")

        self._total_ms = 0
        self._reached_end = False

        # Also reset seek-guard so we don’t get “stuck” thinking we’re mid-seek.
        self._seeking = False
        self._seek_target_ms = None

        self.time_lbl.setText("00:00 / 00:00")

    def _on_tick(self) -> None:
        # This updates the seekbar and time display while playing.
        # Important detail: if the user is scrubbing, we don’t fight them.
        if self._vlc_player.get_media() is None:
            return

        # length can return -1 early on, so we only accept it if it’s legit.
        length = self._vlc_player.get_length()
        if length and length > 0:
            self._total_ms = length

        cur = self._vlc_player.get_time()
        if cur < 0:
            cur = 0

        if self._is_scrubbing and self._pending_seek_ms is not None:
            # While holding the slider, show the scrub time instead of snapping back.
            self.time_lbl.setText(f"{_format_time(self._pending_seek_ms)} / {_format_time(self._total_ms)}")
            return

        if self._seeking and self._seek_target_ms is not None:
            # While VLC is landing, keep the label stable.
            # (Otherwise it looks like it’s teleporting around.)
            self.time_lbl.setText(f"{_format_time(self._seek_target_ms)} / {_format_time(self._total_ms)}")
            return

        if self._total_ms > 0:
            pos = int((cur / self._total_ms) * self.seek.maximum())
            self.seek.blockSignals(True)
            self.seek.setValue(pos)
            self.seek.blockSignals(False)

        self.time_lbl.setText(f"{_format_time(cur)} / {_format_time(self._total_ms)}")

    def _on_slider_pressed(self) -> None:
        # Pause immediately so the video doesn't keep advancing underneath your scrub.
        self._is_scrubbing = True
        self._pending_seek_ms = self._slider_value_to_ms(self.seek.value())

        # Remember if we were playing, so we can resume naturally when you release.
        try:
            self._was_playing_before_scrub = bool(self._vlc_player.is_playing())
        except Exception:
            self._was_playing_before_scrub = False

        try:
            if self._was_playing_before_scrub:
                self._vlc_player.pause()
        except Exception:
            pass

    def _on_slider_value_changed(self, value: int) -> None:
        if not self._is_scrubbing:
            return

        self._pending_seek_ms = self._slider_value_to_ms(value)
        if self._pending_seek_ms is not None:
            self.time_lbl.setText(f"{_format_time(self._pending_seek_ms)} / {_format_time(self._total_ms)}")

    def _on_slider_released(self) -> None:
        # This is the change you asked for:
        # If the video ended, scrubbing back should autoplay without needing you to hit Play.
        if self._pending_seek_ms is None:
            self._is_scrubbing = False
            return

        target = int(self._pending_seek_ms)

        # If we’re in that “EOF dead zone”, we do the restart+seek revive routine.
        if self._reached_end:
            self._restart_and_seek(target, autoplay=True)
        else:
            # Normal case: just seek and resume if we were playing.
            self._commit_seek(target, resume=self._was_playing_before_scrub)

        self._is_scrubbing = False
        self._pending_seek_ms = None

    def _commit_seek(self, target_ms: int, *, resume: bool) -> None:
        # MPD seeks sometimes snap to the previous keyframe/segment boundary.
        # We seek by position when we can (it tends to behave nicer for MPDs),
        # then we "settle" for a moment and update the UI to the real landed time.
        self._seeking = True
        self._seek_target_ms = int(target_ms)
        self.time_lbl.setText(f"{_format_time(self._seek_target_ms)} / {_format_time(self._total_ms)}")

        if self._total_ms > 0:
            try:
                pos = max(0.0, min(1.0, float(target_ms) / float(self._total_ms)))
                self._vlc_player.set_position(pos)
            except Exception:
                # Fallback: set_time also works sometimes, but it’s less reliable for MPD.
                try:
                    self._vlc_player.set_time(int(target_ms))
                except Exception:
                    pass
        else:
            try:
                self._vlc_player.set_time(int(target_ms))
            except Exception:
                pass

        # Let VLC land, then we pick up the real time and continue.
        self._begin_seek_settle(resume=resume)

    def _begin_seek_settle(self, *, resume: bool) -> None:
        # Small settle window so we don't get the "snap back then forward" feeling.
        # Basically: “VLC, take a breath and decide where you actually landed.”
        self._seek_settle_resume = bool(resume)

        if self._seek_settle_timer is not None:
            try:
                self._seek_settle_timer.stop()
            except Exception:
                pass

        self._seek_settle_timer = QTimer(self)
        self._seek_settle_timer.setInterval(60)

        # About 600ms is enough for VLC to land on MPD most of the time.
        self._seek_settle_deadline_ms = 10  # just a counter of ticks
        self._seek_settle_timer.timeout.connect(self._seek_settle_tick)
        self._seek_settle_timer.start()

    def _seek_settle_tick(self) -> None:
        self._seek_settle_deadline_ms -= 1

        landed = self._vlc_player.get_time()
        if landed is None or landed < 0:
            landed = 0

        # Update the UI to the real landed time so the preview matches what you're seeing.
        self._seek_target_ms = int(landed)

        # Stop settling once we've done a few ticks.
        if self._seek_settle_deadline_ms <= 0:
            try:
                self._seek_settle_timer.stop()
            except Exception:
                pass

            # Resume/pause based on what we were doing before the seek.
            if self._seek_settle_resume:
                try:
                    self._vlc_player.play()
                except Exception:
                    pass
                self.play_btn.setText("Pause")
            else:
                self.play_btn.setText("Play")

            self._clear_seek_guard_soon()

    def _clear_seek_guard_soon(self) -> None:
        # Tiny delay so the UI doesn't flicker between old and new times.
        if self._seek_clear_timer is not None:
            try:
                self._seek_clear_timer.stop()
            except Exception:
                pass

        self._seek_clear_timer = QTimer(self)
        self._seek_clear_timer.setSingleShot(True)

        def clear() -> None:
            # After this, _on_tick goes back to being the “source of truth”.
            self._seeking = False
            self._seek_target_ms = None

        self._seek_clear_timer.timeout.connect(clear)
        self._seek_clear_timer.start(200)

    def _slider_value_to_ms(self, value: int) -> Optional[int]:
        # Slider is 0..10_000, so we convert that to a ratio of the total duration.
        if self._total_ms <= 0:
            return None
        ratio = value / float(self.seek.maximum())
        return int(ratio * self._total_ms)

    def _restart_and_seek(self, target_ms: int, *, autoplay: bool) -> None:
        # VLC can get stuck after EOF. We re-arm the player and seek to the target.
        # This is the “revive VLC” routine:
        # stop -> play -> seek -> (optional) resume.
        self._seek_after_restart_ms = max(0, int(target_ms))
        self._resume_after_seek = bool(autoplay)

        # Keep the UI steady while VLC wakes up.
        self._seeking = True
        self._seek_target_ms = int(self._seek_after_restart_ms)
        self.time_lbl.setText(f"{_format_time(self._seek_target_ms)} / {_format_time(self._total_ms)}")

        try:
            self._vlc_player.stop()
        except Exception:
            pass

        self._reached_end = False

        try:
            self._vlc_player.play()
        except Exception:
            pass

        self.stop_btn.setEnabled(True)
        self.play_btn.setText("Play")

        # We try a few times because right after restart VLC sometimes ignores the first seek.
        self._restart_seek_attempts = 0
        QTimer.singleShot(30, self._finish_restart_seek)

    def _finish_restart_seek(self) -> None:
        if self._seek_after_restart_ms is None:
            return

        target = int(self._seek_after_restart_ms)

        ok = False
        try:
            if self._total_ms > 0:
                pos = max(0.0, min(1.0, float(target) / float(self._total_ms)))
                self._vlc_player.set_position(pos)
            else:
                self._vlc_player.set_time(target)
            ok = True
        except Exception:
            ok = False

        self._restart_seek_attempts = getattr(self, "_restart_seek_attempts", 0) + 1

        # If VLC didn’t accept it yet, try again a few times.
        if not ok and self._restart_seek_attempts < 6:
            QTimer.singleShot(40, self._finish_restart_seek)
            return

        self._seek_after_restart_ms = None

        # Autoplay if requested, otherwise pause and wait.
        if self._resume_after_seek:
            try:
                self._vlc_player.play()
            except Exception:
                pass
            self.play_btn.setText("Pause")
        else:
            try:
                self._vlc_player.pause()
            except Exception:
                pass
            self.play_btn.setText("Play")

        # Let it settle quickly so the UI reflects the real landed time.
        self._begin_seek_settle(resume=self._resume_after_seek)

    def _on_vlc_end_reached(self, event) -> None:
        # VLC says “we’re done”. This is the reliable end marker.
        self._reached_end = True
        self.play_btn.setText("Play")

        if self._total_ms > 0:
            self.time_lbl.setText(f"{_format_time(self._total_ms)} / {_format_time(self._total_ms)}")

    def _on_vlc_error(self, event) -> None:
        # Not ideal, but at least we can recover gracefully.
        self._reached_end = True
        self.play_btn.setText("Play")
        self._log("VLC hit a playback error. You can hit Play to restart or scrub and it will recover.")

    def _set_trim_in(self) -> None:
        t = self._vlc_player.get_time()
        if t is None or t < 0:
            return
        self._trim_in_ms = int(t)
        self._update_trim_label()

    def _set_trim_out(self) -> None:
        t = self._vlc_player.get_time()
        if t is None or t < 0:
            return
        self._trim_out_ms = int(t)
        self._update_trim_label()

    def _clear_trim(self) -> None:
        self._trim_in_ms = None
        self._trim_out_ms = None
        self._update_trim_label()

    def _update_trim_label(self) -> None:
        if self._trim_in_ms is None and self._trim_out_ms is None:
            self.trim_lbl.setText("Trim: full")
            return

        a = _format_time(self._trim_in_ms or 0)
        b = _format_time(self._trim_out_ms or (self._total_ms if self._total_ms else 0))
        self.trim_lbl.setText(f"Trim: {a} → {b}")

    def _export_selected(self) -> None:
        item = self.sessions_list.currentItem()
        if not item:
            return

        session: RecordingSession = item.data(Qt.UserRole)
        out_dir = Path(self.out_edit.text()).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        out_mp4 = out_dir / f"{session.name}.mp4"

        trim_start_s = None
        trim_end_s = None
        if self._trim_in_ms is not None:
            trim_start_s = max(0.0, self._trim_in_ms / 1000.0)
        if self._trim_out_ms is not None:
            trim_end_s = max(0.0, self._trim_out_ms / 1000.0)

        if trim_start_s is not None and trim_end_s is not None and trim_end_s <= trim_start_s:
            self._log("Trim looks backwards (Out is before In). Exporting full video instead.")
            trim_start_s, trim_end_s = None, None

        accurate = bool(self.accurate_trim_chk.isChecked())

        try:
            self._log(f"Exporting: {session.mpd_path}")
            self._log(f"Output:    {out_mp4}")

            sig = inspect.signature(export_session)
            kwargs: Dict[str, Any] = {"overwrite": False}

            # Soft-compat with exporter.py: only pass args if it supports them.
            if "start_time" in sig.parameters:
                kwargs["start_time"] = trim_start_s
            if "end_time" in sig.parameters:
                kwargs["end_time"] = trim_end_s
            if "accurate" in sig.parameters:
                kwargs["accurate"] = accurate

            if trim_start_s is not None or trim_end_s is not None:
                self._log(
                    f"Trim request: start={trim_start_s if trim_start_s is not None else 'full'} "
                    f"end={trim_end_s if trim_end_s is not None else 'full'} "
                    f"(accurate={accurate})"
                )

            export_session(session.mpd_path, out_mp4, **kwargs)

            if (trim_start_s is not None or trim_end_s is not None) and (
                "start_time" not in sig.parameters and "end_time" not in sig.parameters
            ):
                self._log("Export finished, but exporter.py doesn’t accept trim args yet, so it exported full length.")

            self._log("Done.")
        except ExportError as e:
            self._log(str(e))
        except TypeError as e:
            self._log(f"Exporter call failed (signature mismatch): {e}")

    def _read_stats_for_current(self) -> None:
        if not self._current_session:
            self._set_stats(MediaStats())
            return

        mpd = self._current_session.mpd_path
        key = str(mpd)
        if key == self._last_stats_key:
            return
        self._last_stats_key = key

        self._log("Reading video stats (ffprobe)…")
        stats = self._probe_stats(mpd)
        self._set_stats(stats)

    def _probe_stats(self, mpd_path: Path) -> MediaStats:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            self._log("ffprobe not found. Stats will stay unknown until we bundle it in the app.")
            return MediaStats(container="DASH (MPD)")

        stats = self._ffprobe_json(ffprobe, mpd_path)
        if stats is None:
            # If MPD probing fails, probe any real segment file.
            # This usually gives correct codec/resolution/audio info.
            seg = self._find_any_segment(mpd_path.parent)
            if seg:
                stats = self._ffprobe_json(ffprobe, seg)

        if stats is None:
            return MediaStats(container="DASH (MPD)")

        return self._stats_from_ffprobe(stats)

    def _ffprobe_json(self, ffprobe: str, path: Path) -> Optional[Dict[str, Any]]:
        try:
            cmd = [
                ffprobe,
                "-v", "error",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                str(path),
            ]
            p = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if p.returncode != 0:
                return None
            return json.loads(p.stdout or "{}")
        except Exception:
            return None

    def _find_any_segment(self, folder: Path) -> Optional[Path]:
        try:
            for ext in (".m4s", ".mp4", ".m4a"):
                hits = sorted(folder.glob(f"*{ext}"))
                if hits:
                    return hits[0]
        except Exception:
            pass
        return None

    def _stats_from_ffprobe(self, data: Dict[str, Any]) -> MediaStats:
        s = MediaStats(container="DASH (MPD)")

        fmt = data.get("format", {}) or {}
        dur = _safe_float(fmt.get("duration"))
        if dur is not None:
            s.duration_s = dur

        streams = data.get("streams", []) or []
        v = None
        a = None
        for st in streams:
            if st.get("codec_type") == "video" and v is None:
                v = st
            if st.get("codec_type") == "audio" and a is None:
                a = st

        if v:
            s.width = _safe_int(v.get("width"))
            s.height = _safe_int(v.get("height"))
            s.vcodec = v.get("codec_name")

            fr = v.get("r_frame_rate") or v.get("avg_frame_rate")
            if isinstance(fr, str) and "/" in fr:
                num, den = fr.split("/", 1)
                try:
                    num_f = float(num)
                    den_f = float(den)
                    if den_f != 0:
                        s.fps = num_f / den_f
                except Exception:
                    pass

        if a:
            s.acodec = a.get("codec_name")
            sr = a.get("sample_rate")
            ch = a.get("channels")
            layout = a.get("channel_layout")
            desc_parts = []
            if layout:
                desc_parts.append(str(layout))
            elif ch:
                desc_parts.append(f"{ch}ch")
            if sr:
                try:
                    desc_parts.append(f"{int(sr)//1000} kHz")
                except Exception:
                    pass
            s.audio_desc = ", ".join(desc_parts) if desc_parts else None

        return s

    def _set_stats(self, stats: MediaStats) -> None:
        self._current_stats = stats

        if stats.duration_s is not None:
            self.stats_duration.setText(f"Duration: {int(stats.duration_s // 60):02d}:{int(stats.duration_s % 60):02d}")
        else:
            self.stats_duration.setText("Duration: —")

        if stats.width and stats.height:
            self.stats_res.setText(f"Resolution: {stats.width}×{stats.height}")
        else:
            self.stats_res.setText("Resolution: —")

        if stats.fps is not None:
            self.stats_fps.setText(f"FPS: {stats.fps:.3f}")
        else:
            self.stats_fps.setText("FPS: —")

        if stats.vcodec:
            self.stats_v.setText(f"Video: {stats.vcodec}")
        else:
            self.stats_v.setText("Video: —")

        if stats.acodec:
            extra = f" ({stats.audio_desc})" if stats.audio_desc else ""
            self.stats_a.setText(f"Audio: {stats.acodec}{extra}")
        else:
            self.stats_a.setText("Audio: —")

        self.stats_container.setText("Container: DASH (MPD)")

        if stats.acodec:
            self.audio_indicator.setText("✅ Audio track detected")
        else:
            self.audio_indicator.setText("❌ No audio detected")

    def _log(self, msg: str) -> None:
        self.log.append(msg)


def run() -> None:
    app = QApplication([])
    w = MainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    run()
