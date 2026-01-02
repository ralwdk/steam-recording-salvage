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

    # Normal export: do a stream copy because it's fast and avoids re-encoding.
    # This matches the original behavior.
    wants_trim = start_time is not None or end_time is not None

    # If the user gave both times, convert that to a duration for ffmpeg.
    # ffmpeg is happiest with -t (duration) once you have a start point.
    duration: Optional[float] = None
    if start_time is not None and end_time is not None and end_time > start_time:
        duration = end_time - start_time

    # If we are not trimming, keep the original fast path.
    if not wants_trim:
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
        _run_ffmpeg(cmd)
        return

    # Trimming has two modes:
    # Fast trim: uses stream copy, but cut points may snap to keyframes.
    # Accurate trim: re-encodes video so the cut is exact.
    #
    # Accurate trim can be slow on CPU, so we try hardware encoders when possible.
    if accurate:
        cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y" if overwrite else "-n",
            "-i",
            str(mpd_path),
        ]

        # Accurate seeking happens when -ss is placed after -i.
        if start_time is not None:
            cmd += ["-ss", f"{start_time:.3f}"]
        if duration is not None:
            cmd += ["-t", f"{duration:.3f}"]
        elif end_time is not None:
            # If someone only gave an end_time, treat it as a duration from 0.
            cmd += ["-t", f"{end_time:.3f}"]

        # Pick a hardware encoder for the current platform.
        # If this fails, we fall back to CPU so exports still work.
        video_args = _pick_video_encoder_args_for_platform()

        cmd_with_gpu = cmd + video_args + [
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]

        try:
            _run_ffmpeg(cmd_with_gpu)
            return
        except ExportError:
            # If the GPU encoder fails (driver, unsupported build, etc),
            # retry once using normal CPU x264.
            cmd_with_cpu = cmd + [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output_mp4),
            ]
            _run_ffmpeg(cmd_with_cpu)
            return

    # Fast trim path (keyframe-ish) keeps stream copy, still very quick.
    # Placing -ss before -i is the common fast-seek approach.
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y" if overwrite else "-n",
    ]

    if start_time is not None:
        cmd += ["-ss", f"{start_time:.3f}"]

    cmd += ["-i", str(mpd_path)]

    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    elif end_time is not None:
        cmd += ["-t", f"{end_time:.3f}"]

    cmd += [
        "-c",
        "copy",
        str(output_mp4),
    ]

    _run_ffmpeg(cmd)


def _pick_video_encoder_args_for_platform() -> list:
    # macOS uses VideoToolbox, which is available by default and fast.
    if sys.platform == "darwin":
        return ["-c:v", "h264_videotoolbox"]

    # On Windows, try common GPU encoders in a reasonable order.
    # First one that exists in the ffmpeg build wins.
    if sys.platform.startswith("win"):
        for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
            if _ffmpeg_supports_encoder(enc):
                return ["-c:v", enc]

        # Nothing usable found, fall back to CPU.
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]

    # Other platforms stick to CPU for now.
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def _ffmpeg_supports_encoder(encoder_name: str) -> bool:
    # Simple check to see if this ffmpeg build advertises the encoder.
    found = shutil.which("ffmpeg")
    if not found:
        return False

    try:
        result = subprocess.run(
            [found, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and encoder_name in (result.stdout or "")
    except Exception:
        return False


def _run_ffmpeg(cmd: list) -> None:
    # Small helper so all ffmpeg errors show up the same way.
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise ExportError(
            "ffmpeg failed while exporting the session.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
