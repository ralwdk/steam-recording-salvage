import shutil
import subprocess
import sys
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


def _seconds_to_ffmpeg_time(seconds: float) -> str:
    # Keep ffmpeg happy (it accepts "12.345" seconds fine).
    if seconds < 0:
        seconds = 0.0
    return f"{seconds:.3f}"


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

    # Figure out how long the user actually wants.
    # We treat end_time as "stop at this time", not "duration".
    duration: Optional[float] = None
    if start_time is not None and end_time is not None:
        if end_time <= start_time:
            # If the caller messed up trim ordering, just ignore trim.
            start_time, end_time = None, None
        else:
            duration = end_time - start_time
    elif start_time is None and end_time is not None:
        # "from 0 to end"
        duration = max(0.0, end_time)

    # Decide trim style:
    # Fast trim = stream copy (super fast, but cut may land on a nearby keyframe)
    # Accurate trim = re-encode (slower, but exact cut)
    do_trim = (start_time is not None) or (duration is not None)
    use_reencode = bool(accurate and do_trim)

    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
    ]

    if not use_reencode:
        # Fast path: try stream copy because it's quick and avoids re-encoding.
        # We put -ss BEFORE -i for speed.
        if start_time is not None:
            cmd += ["-ss", _seconds_to_ffmpeg_time(start_time)]
        cmd += ["-i", str(mpd_path)]
        if duration is not None:
            cmd += ["-t", _seconds_to_ffmpeg_time(duration)]

        cmd += [
            "-c",
            "copy",
            str(output_mp4),
        ]
    else:
        # Accurate path: we re-encode so the cut is exact.
        # We put -ss AFTER -i for accuracy (ffmpeg can’t skip decode this way).
        cmd += ["-i", str(mpd_path)]
        if start_time is not None:
            cmd += ["-ss", _seconds_to_ffmpeg_time(start_time)]
        if duration is not None:
            cmd += ["-t", _seconds_to_ffmpeg_time(duration)]

        # On macOS, prefer VideoToolbox (GPU) so accurate trims are much faster.
        # On other platforms, fall back to normal x264.
        if sys.platform == "darwin":
            # This is the main speedup you wanted for accurate trim exports.
            cmd += [
                "-c:v",
                "h264_videotoolbox",
                # Simple bitrate target. If you want, we can expose this later.
                "-b:v",
                "20M",
            ]
        else:
            cmd += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
            ]

        # Keep audio reasonable and compatible.
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_mp4),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise ExportError(
            "ffmpeg failed while exporting the session.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
