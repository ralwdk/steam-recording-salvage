import shutil
import subprocess
from pathlib import Path
from typing import Optional


class ExportError(RuntimeError):
    pass


def resolve_ffmpeg(custom_path: Optional[Path]) -> Path:
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
    ffmpeg_path: Optional[Path] = None,
    overwrite: bool = False,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    accurate: bool = False,
) -> None:
    mpd_path = mpd_path.expanduser().resolve()
    output_mp4 = output_mp4.expanduser().resolve()

    if not mpd_path.exists():
        raise ExportError(f"session.mpd not found: {mpd_path}")

    ffmpeg = resolve_ffmpeg(ffmpeg_path)
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Clean up trim values so we don’t pass bad input to ffmpeg.
    if start_time is not None and start_time < 0:
        start_time = 0.0
    if end_time is not None and end_time < 0:
        end_time = 0.0
    if start_time is not None and end_time is not None and end_time <= start_time:
        start_time, end_time = None, None

    duration = None
    if start_time is not None and end_time is not None:
        duration = max(0.0, end_time - start_time)

    # Build the ffmpeg command.
    # If no trim is requested, we keep it simple and fast.
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
    ]

    if start_time is None and duration is None:
        # No trimming. Just copy streams directly.
        cmd += [
            "-i",
            str(mpd_path),
            "-c",
            "copy",
            str(output_mp4),
        ]
    else:
        if not accurate:
            # Fast trim.
            # This is quick and keeps the original streams,
            # but cuts may land on nearby keyframes.
            if start_time is not None:
                cmd += ["-ss", f"{start_time:.3f}"]
            cmd += ["-i", str(mpd_path)]
            if duration is not None:
                cmd += ["-t", f"{duration:.3f}"]
            cmd += ["-c", "copy", str(output_mp4)]
        else:
            # Accurate trim.
            # Slower, but re-encodes so the cut is exact.
            cmd += ["-i", str(mpd_path)]
            if start_time is not None:
                cmd += ["-ss", f"{start_time:.3f}"]
            if duration is not None:
                cmd += ["-t", f"{duration:.3f}"]

            # Reasonable defaults for quality and compatibility.
            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(output_mp4),
            ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise ExportError(
            "ffmpeg failed while exporting the session.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
