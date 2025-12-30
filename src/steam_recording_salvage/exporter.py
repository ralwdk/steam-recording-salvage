import shutil
import subprocess
from pathlib import Path


class ExportError(RuntimeError):
    pass


def resolve_ffmpeg(custom_path: Path | None) -> Path:
    # Prefer an explicit ffmpeg path if provided, otherwise fall back to PATH.
    if custom_path:
        ffmpeg = custom_path.expanduser()
        if ffmpeg.exists():
            return ffmpeg
        raise ExportError(f"ffmpeg not found at {ffmpeg}")

    found = shutil.which("ffmpeg")
    if found:
        return Path(found)

    raise ExportError("ffmpeg not found. Install it or pass --ffmpeg explicitly.")


def export_session(
    mpd_path: Path,
    output_mp4: Path,
    *,
    ffmpeg_path: Path | None = None,
    overwrite: bool = False,
) -> None:
    mpd_path = mpd_path.expanduser().resolve()
    output_mp4 = output_mp4.expanduser().resolve()

    if not mpd_path.exists():
        raise ExportError(f"session.mpd not found: {mpd_path}")

    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    # We try stream copy first because it's fast and avoids re-encoding.
    # If this fails for certain sessions, we can add a fallback later.
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
        "-i",
        str(mpd_path),
        "-c",
        "copy",
        str(output_mp4),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise ExportError(
            "ffmpeg failed while exporting the session.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
