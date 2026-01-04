from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

import vlc
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QCloseEvent
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
    QStyle,
)

from .scan import discover_sessions, find_sessions, RecordingSession
from .exporter import export_session, ExportError
from .steam_titles import build_steam_appid_title_map_with_cache


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
        # None means "I genuinely don't know yet"
        # True means "audio stream exists"
        if self.acodec is None:
            return None
        return True


def _format_time(ms: int) -> str:
    # Defensive: VLC sometimes gives -1 for time when it's starting/stopping.
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
        # This is best-effort, and it doesn't break anything if VLC ignores it.
        # Also: no-video-title-show removes that random "filename" overlay VLC can do.
        self._vlc_instance = vlc.Instance(
            "--no-video-title-show",
            "--file-caching=2000",
            "--network-caching=2000",
        )
        self._vlc_player = self._vlc_instance.media_player_new()

        # Hook VLC events so we know when it REALLY ended (time math isn't reliable here).
        # MPD playback can "look ended" without time==length, so this is the reliable signal.
        self._vlc_events = self._vlc_player.event_manager()
        self._vlc_events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_vlc_end_reached)
        self._vlc_events.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_vlc_error)

        self._current_session: Optional[RecordingSession] = None
        self._current_stats: MediaStats = MediaStats()

        # Scrub state: while you're dragging the slider, we treat it like "preview mode"
        # and we don't let the timer yank the slider around.
        self._is_scrubbing = False
        self._pending_seek_ms: Optional[int] = None

        # Total duration cache (ms). VLC can be a little slow to provide it for MPDs.
        self._total_ms: int = 0

        # Tiny cache so we don't re-run ffprobe if you re-select the same item.
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
        # Without this, _on_tick can overwrite the label/slider while we're mid-seek.
        self._seeking = False
        self._seek_target_ms: Optional[int] = None
        self._seek_clear_timer: Optional[QTimer] = None

        # Seek settling tries (MPD seeks land on keyframes/segment boundaries sometimes).
        # So we "settle" for a few ticks, then accept whatever time VLC actually landed on.
        self._seek_settle_timer: Optional[QTimer] = None
        self._seek_settle_deadline_ms: int = 0  # not real milliseconds, just a tick counter
        self._seek_settle_resume: bool = False

        # Cache of Steam AppID -> title, built from local Steam files.
        # I keep this around so we don't keep hammering disk every time we refresh the list.
        self._appid_to_title: Dict[int, str] = {}

        # If we can't resolve a title, I still want grouping to work.
        # So we show a fallback label instead of just dumping numeric IDs everywhere.
        self._unknown_title_prefix = "Steam App"

        self.root_edit = QLineEdit()
        self.browse_root_btn = QPushButton("Browse…")

        # This label sits above the sessions list.
        # I like it because it gives context when you're pointed at a userdata folder.
        self.sessions_title_lbl = QLabel("Sessions found")

        self.sessions_list = QListWidget()

        self.video_frame = QFrame()
        self.video_frame.setFrameShape(QFrame.StyledPanel)
        self.video_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_frame.setMinimumHeight(420)  # I like the preview feeling "taller"

        self.play_btn = QPushButton("Play")

        # Stop button ended up feeling kind of pointless here.
        # We already have Play/Pause, scrubbing, and session switching resets playback anyway.
        # So I'm swapping Stop for volume controls like a normal media player.
        self.mute_btn = QPushButton()
        self.mute_btn.setToolTip("Mute / unmute")

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(100)
        self.volume.setSingleStep(2)
        self.volume.setPageStep(10)
        self.volume.setToolTip("Volume")

        # Remember last non-zero volume so mute/unmute behaves like you'd expect.
        self._last_nonzero_volume: int = 100

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
        # We keep it smooth, but not "spammy".
        self._tick = QTimer(self)
        self._tick.setInterval(120)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

        # Make sure the icon matches the default slider state right away.
        self._refresh_volume_icon()
        self._apply_volume_to_vlc()

    def _apply_compact_ui(self) -> None:
        # Smaller font so the minimum size can still look clean without overlaps.
        f = QFont()
        f.setPointSize(12)
        self.setFont(f)

        # Keep buttons tight so the preview gets more space.
        for b in [
            self.play_btn,
            self.mute_btn,
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

        # Compact volume control so it doesn't steal space from the seek bar.
        self.volume.setMinimumHeight(20)
        self.volume.setFixedWidth(140)

        # Square-ish button so the icon looks clean.
        self.mute_btn.setFixedWidth(36)

    def showEvent(self, event) -> None:
        super().showEvent(event)

        # Lock the minimum size to whatever we open at.
        # So users can't shrink it into a broken layout.
        self.setMinimumSize(self.size())

    def closeEvent(self, event: QCloseEvent) -> None:
        """Clean up VLC resources when window closes."""
        try:
            # Stop all timers
            if self._tick and self._tick.isActive():
                self._tick.stop()
            
            if self._seek_settle_timer and self._seek_settle_timer.isActive():
                self._seek_settle_timer.stop()
                
            if self._seek_clear_timer and self._seek_clear_timer.isActive():
                self._seek_clear_timer.stop()
            
            # Release VLC player
            if self._vlc_player:
                try:
                    self._vlc_player.stop()
                except Exception:
                    pass
                
                # Detach events
                if self._vlc_events:
                    try:
                        self._vlc_events.event_detach(vlc.EventType.MediaPlayerEndReached)
                        self._vlc_events.event_detach(vlc.EventType.MediaPlayerEncounteredError)
                    except Exception:
                        pass
                    
                try:
                    self._vlc_player.release()
                except Exception:
                    pass
            
            # Release VLC instance
            if self._vlc_instance:
                try:
                    self._vlc_instance.release()
                except Exception:
                    pass
                    
        except Exception as e:
            print(f"Error during cleanup: {e}")
        
        finally:
            event.accept()

    def _build_layout(self) -> None:
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Recordings root"))
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self.browse_root_btn)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Export to"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.browse_out_btn)

        left_col = QVBoxLayout()
        left_col.addWidget(self.sessions_title_lbl)
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

        right_col = QVBoxLayout()
        right_col.addWidget(QLabel("Preview"))
        right_col.addWidget(self.video_frame, 1)

        controls_row = QHBoxLayout()
        controls_row.addWidget(self.play_btn)
        controls_row.addWidget(self.mute_btn)
        controls_row.addWidget(self.volume)
        controls_row.addWidget(self.seek, 1)
        controls_row.addWidget(self.time_lbl)
        right_col.addLayout(controls_row)

        trim_row = QHBoxLayout()
        trim_row.addWidget(self.trim_lbl)
        trim_row.addStretch(1)
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

        # Volume controls are intentionally simple: slider sets the volume, button toggles mute.
        self.volume.valueChanged.connect(self._on_volume_changed)
        self.mute_btn.clicked.connect(self._toggle_mute)

        self.seek.sliderPressed.connect(self._on_slider_pressed)
        self.seek.sliderReleased.connect(self._on_slider_released)
        self.seek.valueChanged.connect(self._on_slider_value_changed)

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

    def _extract_appid_from_session(self, s: RecordingSession) -> Optional[int]:
        # Steam sessions live under a folder name like: bg_<appid>_<timestamp>...
        # Example: bg_1903340_20260101_012808
        # So I just yank out the middle number and call it a day.
        try:
            folder = s.mpd_path.parent.name
            parts = folder.split("_")
            if len(parts) >= 2 and parts[0] in {"bg", "fg"}:
                return int(parts[1])
        except Exception:
            pass
        return None

    def _ensure_steam_titles_loaded(self) -> None:
        # This tries to resolve AppIDs to titles using local Steam install data.
        # If it fails, no big deal, we still show AppIDs as a fallback.
        if self._appid_to_title:
            return

        try:
            res = build_steam_appid_title_map_with_cache()
            self._appid_to_title = dict(res.appid_to_title)
        except Exception:
            self._appid_to_title = {}

    def _refresh_sessions(self) -> None:
        self.sessions_list.clear()
        self._current_session = None
        self.export_btn.setEnabled(False)
        self.play_btn.setEnabled(False)

        # When switching roots, we're basically resetting the whole player state.
        self._stop_preview()
        self._clear_trim()
        self._set_stats(MediaStats())

        root = Path(self.root_edit.text()).expanduser()
        sessions = find_sessions(root) if root.exists() else []

        if not sessions:
            self.sessions_title_lbl.setText("Sessions found")
            self._log(f"No sessions found under: {root}")
            return

        # Load titles once per app run (best-effort). If this fails, we just show AppIDs.
        self._ensure_steam_titles_loaded()

        # Group sessions by AppID so each game gets its own header section.
        grouped: Dict[int, List[RecordingSession]] = {}
        unknown_bucket: List[RecordingSession] = []

        for s in sessions:
            appid = self._extract_appid_from_session(s)
            if appid is None:
                # If Steam changes formats or something weird happens, I don't want the whole UI to die.
                unknown_bucket.append(s)
                continue
            grouped.setdefault(appid, []).append(s)

        # Keep the label generic because we're showing multiple games.
        self.sessions_title_lbl.setText(f"Sessions found ({len(sessions)})")

        # Sort games by title (nice and readable), falling back to AppID string.
        def game_sort_key(appid: int) -> str:
            title = self._appid_to_title.get(appid)
            if title:
                return title.casefold()
            return f"{appid:010d}"

        for appid in sorted(grouped.keys(), key=game_sort_key):
            title = self._appid_to_title.get(appid) or f"{self._unknown_title_prefix} {appid}"

            # Header row. I make this non-selectable so you can't accidentally "play" a header.
            header = QListWidgetItem(title)
            header.setFlags(header.flags() & ~Qt.ItemIsSelectable)

            # I want headers to visually read like sections, not like clickable items.
            f = header.font()
            f.setBold(True)
            header.setFont(f)

            self.sessions_list.addItem(header)

            # Add the sessions under that header.
            # I indent them so it feels like a hierarchy, without needing a whole tree widget.
            for s in sorted(grouped[appid], key=lambda x: x.mpd_path.as_posix()):
                # I only want the list to show the "human" part:
                # bg_1903340_.../session.mpd instead of the full C:/Program Files/... monster path.
                pretty = f"{s.mpd_path.parent.name}/{s.mpd_path.name}"
                row = QListWidgetItem(f"    {pretty}")
                row.setData(Qt.UserRole, s)
                self.sessions_list.addItem(row)

        # If we couldn't parse AppID for some sessions, dump them at the end.
        if unknown_bucket:
            header = QListWidgetItem("Other recordings (unmatched)")
            header.setFlags(header.flags() & ~Qt.ItemIsSelectable)

            f = header.font()
            f.setBold(True)
            header.setFont(f)

            self.sessions_list.addItem(header)

            for s in sorted(unknown_bucket, key=lambda x: x.mpd_path.as_posix()):
                pretty = f"{s.mpd_path.parent.name}/{s.mpd_path.name}"
                row = QListWidgetItem(f"    {pretty}")
                row.setData(Qt.UserRole, s)
                self.sessions_list.addItem(row)

        self._log(f"Found {len(sessions)} session(s). Select one to preview or export.")

    def _on_selection_changed(self) -> None:
        item = self.sessions_list.currentItem()
        if not item:
            self._current_session = None
            self.export_btn.setEnabled(False)
            self.play_btn.setEnabled(False)
            return

        session = item.data(Qt.UserRole)

        # If you clicked a header row, there's no session attached, so we just do nothing.
        if session is None:
            self.sessions_list.clearSelection()
            return

        self._current_session = session
        self.export_btn.setEnabled(True)
        self.play_btn.setEnabled(True)

        # Reset trim on new selection so you don't accidentally export the wrong range.
        self._clear_trim()

        # New video = fresh end-state.
        self._reached_end = False

        # Auto start preview to match Steam-like feel.
        self._start_preview()

        # Pull stats as soon as we select something.
        self._read_stats_for_current()

    def _attach_vlc_to_widget(self) -> None:
        # VLC needs the native window handle, and it's different per OS.
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

        media = self._vlc_instance.media_new(str(mpd))
        self._vlc_player.set_media(media)

        ok = self._vlc_player.play()
        if ok == -1:
            self._log("VLC failed to start playback. (libVLC not found / VLC not installed?)")
            return

        self.play_btn.setText("Pause")
        self._reached_end = False

        # VLC sometimes resets audio when new media loads, so I just re-apply volume here.
        self._apply_volume_to_vlc()

        self._log(f"Previewing: {mpd}")

    def _toggle_play_pause(self) -> None:
        if self._vlc_player.is_playing():
            self._vlc_player.pause()
            self.play_btn.setText("Play")
            return

        if self._current_session and self._vlc_player.get_media() is None:
            self._start_preview()
            return

        if self._reached_end:
            self._restart_and_seek(0, autoplay=True)
            return

        self._vlc_player.play()
        self.play_btn.setText("Pause")

    def _stop_preview(self) -> None:
        # Still useful internally when switching sessions/roots.
        try:
            self._vlc_player.stop()
        except Exception:
            pass

        self.play_btn.setText("Play")

        self._total_ms = 0
        self._reached_end = False

        self._seeking = False
        self._seek_target_ms = None

        self.time_lbl.setText("00:00 / 00:00")

    def _on_tick(self) -> None:
        if self._vlc_player.get_media() is None:
            return

        length = self._vlc_player.get_length()
        if length and length > 0:
            self._total_ms = length

        cur = self._vlc_player.get_time()
        if cur < 0:
            cur = 0

        if self._is_scrubbing and self._pending_seek_ms is not None:
            self.time_lbl.setText(f"{_format_time(self._pending_seek_ms)} / {_format_time(self._total_ms)}")
            return

        if self._seeking and self._seek_target_ms is not None:
            self.time_lbl.setText(f"{_format_time(self._seek_target_ms)} / {_format_time(self._total_ms)}")
            return

        if self._total_ms > 0:
            pos = int((cur / self._total_ms) * self.seek.maximum())
            self.seek.blockSignals(True)
            self.seek.setValue(pos)
            self.seek.blockSignals(False)

        self.time_lbl.setText(f"{_format_time(cur)} / {_format_time(self._total_ms)}")

    def _on_slider_pressed(self) -> None:
        self._is_scrubbing = True
        self._pending_seek_ms = self._slider_value_to_ms(self.seek.value())

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
        if self._pending_seek_ms is None:
            self._is_scrubbing = False
            return

        target = int(self._pending_seek_ms)

        if self._reached_end:
            self._restart_and_seek(target, autoplay=True)
        else:
            self._commit_seek(target, resume=self._was_playing_before_scrub)

        self._is_scrubbing = False
        self._pending_seek_ms = None

    def _commit_seek(self, target_ms: int, *, resume: bool) -> None:
        self._seeking = True
        self._seek_target_ms = int(target_ms)
        self.time_lbl.setText(f"{_format_time(self._seek_target_ms)} / {_format_time(self._total_ms)}")

        if self._total_ms > 0:
            try:
                pos = max(0.0, min(1.0, float(target_ms) / float(self._total_ms)))
                self._vlc_player.set_position(pos)
            except Exception:
                try:
                    self._vlc_player.set_time(int(target_ms))
                except Exception:
                    pass
        else:
            try:
                self._vlc_player.set_time(int(target_ms))
            except Exception:
                pass

        self._begin_seek_settle(resume=resume)

    def _begin_seek_settle(self, *, resume: bool) -> None:
        self._seek_settle_resume = bool(resume)

        if self._seek_settle_timer is not None:
            try:
                self._seek_settle_timer.stop()
            except Exception:
                pass

        self._seek_settle_timer = QTimer(self)
        self._seek_settle_timer.setInterval(60)

        self._seek_settle_deadline_ms = 10
        self._seek_settle_timer.timeout.connect(self._seek_settle_tick)
        self._seek_settle_timer.start()

    def _seek_settle_tick(self) -> None:
        self._seek_settle_deadline_ms -= 1

        landed = self._vlc_player.get_time()
        if landed is None or landed < 0:
            landed = 0

        self._seek_target_ms = int(landed)

        if self._seek_settle_deadline_ms <= 0:
            try:
                self._seek_settle_timer.stop()
            except Exception:
                pass

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
        if self._seek_clear_timer is not None:
            try:
                self._seek_clear_timer.stop()
            except Exception:
                pass

        self._seek_clear_timer = QTimer(self)
        self._seek_clear_timer.setSingleShot(True)

        def clear() -> None:
            self._seeking = False
            self._seek_target_ms = None

        self._seek_clear_timer.timeout.connect(clear)
        self._seek_clear_timer.start(200)

    def _slider_value_to_ms(self, value: int) -> Optional[int]:
        if self._total_ms <= 0:
            return None
        ratio = value / float(self.seek.maximum())
        return int(ratio * self._total_ms)

    def _restart_and_seek(self, target_ms: int, *, autoplay: bool) -> None:
        self._seek_after_restart_ms = max(0, int(target_ms))
        self._resume_after_seek = bool(autoplay)

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

        # Re-apply volume after restart because VLC can be weird about audio state.
        self._apply_volume_to_vlc()

        self.play_btn.setText("Play")

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

        if not ok and self._restart_seek_attempts < 6:
            QTimer.singleShot(40, self._finish_restart_seek)
            return

        self._seek_after_restart_ms = None

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

        self._begin_seek_settle(resume=self._resume_after_seek)

    def _on_vlc_end_reached(self, event) -> None:
        self._reached_end = True
        self.play_btn.setText("Play")

        if self._total_ms > 0:
            self.time_lbl.setText(f"{_format_time(self._total_ms)} / {_format_time(self._total_ms)}")

    def _on_vlc_error(self, event) -> None:
        self._reached_end = True
        self.play_btn.setText("Play")
        self._log("VLC hit a playback error. You can hit Play to restart or scrub and it will recover.")

    def _apply_volume_to_vlc(self) -> None:
        # VLC volume is 0..100, so this is a nice boring direct mapping.
        try:
            vol = int(self.volume.value())
            self._vlc_player.audio_set_volume(vol)
        except Exception:
            pass

        # Using slider==0 as "muted" keeps behaviour simple.
        try:
            self._vlc_player.audio_set_mute(self.volume.value() == 0)
        except Exception:
            pass

        self._refresh_volume_icon()

    def _refresh_volume_icon(self) -> None:
        # Standard Qt icons so it looks normal on Windows/Mac/Linux.
        if self.volume.value() == 0:
            icon = self.style().standardIcon(QStyle.SP_MediaVolumeMuted)
        else:
            icon = self.style().standardIcon(QStyle.SP_MediaVolume)
        self.mute_btn.setIcon(icon)

    def _on_volume_changed(self, value: int) -> None:
        # If the user drags volume up, I treat that as "unmute".
        if value > 0:
            self._last_nonzero_volume = int(value)

        self._apply_volume_to_vlc()

    def _toggle_mute(self) -> None:
        # Mute sets slider to 0, unmute restores the last non-zero volume.
        if self.volume.value() == 0:
            self.volume.setValue(max(1, int(self._last_nonzero_volume or 100)))
        else:
            self._last_nonzero_volume = int(self.volume.value())
            self.volume.setValue(0)

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

        session: Optional[RecordingSession] = item.data(Qt.UserRole)
        if session is None:
            return

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
                self._log("Export finished, but exporter.py doesn't accept trim args yet, so it exported full length.")

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