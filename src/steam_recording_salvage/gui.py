from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
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

        self._wire_ui()
        self._set_defaults()
        self._refresh_sessions()

    def _wire_ui(self) -> None:
        # Top: recordings root
        root_row = QHBoxLayout()
        root_row.addWidget(QLabel("Recordings root"))
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self.browse_root_btn)

        # Middle: sessions list
        sessions_col = QVBoxLayout()
        sessions_col.addWidget(QLabel("Sessions found"))
        sessions_col.addWidget(self.sessions_list, 1)

        # Output row
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Export to"))
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.browse_out_btn)

        # Bottom: export + log
        bottom_col = QVBoxLayout()
        bottom_col.addWidget(self.export_btn)
        bottom_col.addWidget(QLabel("Log"))
        bottom_col.addWidget(self.log, 1)

        layout = QVBoxLayout()
        layout.addLayout(root_row)
        layout.addLayout(sessions_col)
        layout.addLayout(out_row)
        layout.addLayout(bottom_col)
        self.setLayout(layout)

        # Signals
        self.browse_root_btn.clicked.connect(self._choose_root)
        self.browse_out_btn.clicked.connect(self._choose_out_dir)
        self.root_edit.editingFinished.connect(self._refresh_sessions)
        self.sessions_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.export_btn.clicked.connect(self._export_selected)

    def _set_defaults(self) -> None:
        # Start with a best-effort auto-detect; user can always override.
        roots = [s.mpd_path.parents[3] for s in discover_sessions()]  # userdata-ish roots
        if roots:
            self.root_edit.setText(str(roots[0]))
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

        root = Path(self.root_edit.text()).expanduser()
        sessions = find_sessions(root) if root.exists() else []

        if not sessions:
            self._log(f"No sessions found under: {root}")
            self.export_btn.setEnabled(False)
            return

        for s in sessions:
            item = QListWidgetItem(s.mpd_path.as_posix())
            item.setData(Qt.UserRole, s)
            self.sessions_list.addItem(item)

        self._log(f"Found {len(sessions)} session(s). Select one to export.")
        self.export_btn.setEnabled(False)

    def _on_selection_changed(self) -> None:
        self.export_btn.setEnabled(self.sessions_list.currentItem() is not None)

    def _export_selected(self) -> None:
        item = self.sessions_list.currentItem()
        if not item:
            return

        session: RecordingSession = item.data(Qt.UserRole)
        out_dir = Path(self.out_edit.text()).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # Use the session folder name as a simple default output name.
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
    w.resize(900, 650)
    w.show()
    app.exec()


if __name__ == "__main__":
    run()
EOF
