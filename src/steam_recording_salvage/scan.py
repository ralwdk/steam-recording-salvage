from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecordingSession:
    mpd_path: Path
    folder: Path

    @property
    def name(self) -> str:
        return self.folder.name


def default_userdata_roots() -> list[Path]:
    # Common Steam userdata locations across platforms.
    # Users can always override this manually if Steam is installed elsewhere.
    roots = [
        Path(r"C:\Program Files (x86)\Steam\userdata"),
        Path(r"C:\Program Files\Steam\userdata"),
        Path.home() / "AppData" / "Local" / "Steam" / "userdata",
        Path.home() / "Library" / "Application Support" / "Steam" / "userdata",
        Path.home() / ".steam" / "steam" / "userdata",
        Path.home() / ".local" / "share" / "Steam" / "userdata",
    ]
    return [p for p in roots if p.exists()]


def find_sessions(root: Path) -> list[RecordingSession]:
    # Steam recordings are stored as DASH sessions.
    # Each session folder contains a session.mpd manifest.
    root = root.expanduser().resolve()
    if not root.exists():
        return []

    sessions: list[RecordingSession] = []

    for mpd in root.rglob("session.mpd"):
        sessions.append(
            RecordingSession(
                mpd_path=mpd,
                folder=mpd.parent,
            )
        )

    # Keep results stable and predictable
    sessions.sort(key=lambda s: str(s.mpd_path).lower())
    return sessions


def discover_sessions() -> list[RecordingSession]:
    # Scan all known Steam userdata roots and merge results.
    all_sessions: list[RecordingSession] = []

    for root in default_userdata_roots():
        all_sessions.extend(find_sessions(root))

    # Remove duplicates in case multiple roots overlap
    seen = set()
    unique: list[RecordingSession] = []

    for session in all_sessions:
        if session.mpd_path in seen:
            continue
        seen.add(session.mpd_path)
        unique.append(session)

    return unique
