from __future__ import annotations
from typing import Optional

import sys
from pathlib import Path

import vlc
from PySide6.QtCore import Qt
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .scan import discover_sessions, find_sessions, RecordingSession
from .exporter import export_session, ExportError


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Steam Recording Salvage")

        self.root_edit = QLineEdit()
        self.browse_root_btn = QPushButton("Browse…")

        self.sessions_list = QListWidget()

        self.out_edit = QLineEdit()
        self.browse_out_btn = QPushButton("Browse…")

        self.export_btn = QPushButton("Export")
        self.export_btn.setEnabled(False)

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        # VLC preview
        self.video_frame = QFrame()
        self.video_frame.setFrameShape(QFrame.StyledPanel)
        self.play_btn = QPushButton("Play Preview")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self._vlc_instance = vlc.Instance()
        self._vlc_player = self._vlc_instance.media_player_new()
        self._current_session: Optional[RecordingSession] = None

        self._wire_ui()
        self._set_defaults()
        self._refresh_sessions()

    def _wire_ui(self) -> None:
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Recordings root"))
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self.browse_root_btn)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Export to"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.browse_out_btn)

        preview_btns = QHBoxLayout()
        preview_btns.addWidget(self.play_btn)
        preview_btns.addWidget(self.stop_btn)
        preview_btns.addStretch(1)

        preview_col = QVBoxLayout()
        preview_col.addWidget(QLabel("Preview"))
        preview_col.addWidget(self.video_frame, 1)
        preview_col.addLayout(preview_btns)

        sessions_col = QVBoxLayout()
        sessions_col.addWidget(QLabel("Sessions found"))
        sessions_col.addWidget(self.sessions_list, 1)

        mid_row = QHBoxLayout()
        mid_row.addLayout(sessions_col, 1)
        mid_row.addLayout(preview_col, 1)

        bottom_col = QVBoxLayout()
        bottom_col.addWidget(self.export_btn)
        bottom_col.addWidget(QLabel("Log"))
        bottom_col.addWidget(self.log, 1)

        layout = QVBoxLayout()
        layout.addLayout(root_row)
        layout.addLayout(mid_row)
        layout.addLayout(out_row)
        layout.addLayout(bottom_col)
        self.setLayout(layout)

        self.browse_root_btn.clicked.connect(self._choose_root)
        self.browse_out_btn.clicked.connect(self._choose_out_dir)
        self.root_edit.editingFinished.connect(self._refresh_sessions)
        self.sessions_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.export_btn.clicked.connect(self._export_selected)

        self.play_btn.clicked.connect(self._play_selected_preview)
        self.stop_btn.clicked.connect(self._stop_preview)

    def _set_defaults(self) -> None:
        sessions = discover_sessions()
        if sessions:
            # Best-effort: jump to something near where sessions were found
            self.root_edit.setText(str(sessions[0].mpd_path.parents[3]))
        else:
            self.root_edit.setText(str(Path.home()))

        self.out_edit.setText(str(Path.home() / "Desktop"))

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
        self._stop_preview()

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

        # Auto-load preview when you select something (Steam-like feel)
        self._play_selected_preview()

    def _attach_vlc_to_widget(self) -> None:
        wid = int(self.video_frame.winId())

        if sys.platform.startswith("win"):
            self._vlc_player.set_hwnd(wid)
        elif sys.platform == "darwin":
            self._vlc_player.set_nsobject(wid)
        else:
            self._vlc_player.set_xwindow(wid)

    def _play_selected_preview(self) -> None:
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

        self.stop_btn.setEnabled(True)
        self._log(f"Previewing: {mpd}")

    def _stop_preview(self) -> None:
        try:
            self._vlc_player.stop()
        except Exception:
            pass
        self.stop_btn.setEnabled(False)

    def _export_selected(self) -> None:
        item = self.sessions_list.currentItem()
        if not item:
            return

        session: RecordingSession = item.data(Qt.UserRole)
        out_dir = Path(self.out_edit.text()).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        out_mp4 = out_dir / f"{session.name}.mp4"

        try:
            self._log(f"Exporting: {session.mpd_path}")
            self._log(f"Output:    {out_mp4}")
            export_session(session.mpd_path, out_mp4, overwrite=False)
            self._log("Done.")
        except ExportError as e:
            self._log(str(e))

    def _log(self, msg: str) -> None:
        self.log.append(msg)


def run() -> None:
    app = QApplication([])
    w = MainWindow()
    w.resize(1100, 700)
    w.show()
    app.exec()


if __name__ == "__main__":
    run()
