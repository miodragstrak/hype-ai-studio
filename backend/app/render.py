import json
import math
import subprocess
from pathlib import Path

from backend.app.config import settings


def probe_duration(path: Path) -> float:
    probe = subprocess.run(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        raise RuntimeError(probe.stderr[-1000:])
    duration = float(json.loads(probe.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError("media duration is invalid")
    return duration


def compose_video(
    input_paths: list[Path],
    audio_path: Path,
    output_path: Path,
    audio_start_seconds: float = 0,
) -> float:
    if not input_paths:
        raise ValueError("at least one video input is required")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[str] = []
    for path in input_paths:
        inputs += ["-i", str(path)]
    inputs += ["-ss", str(audio_start_seconds), "-i", str(audio_path)]
    video_inputs = "".join(f"[{index}:v:0]" for index in range(len(input_paths)))
    filter_graph = f"{video_inputs}concat=n={len(input_paths)}:v=1:a=0[v]"
    command = [
        settings.ffmpeg_executable,
        "-y",
        *inputs,
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
        "-map",
        f"{len(input_paths)}:a:0",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(output_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-1000:])
    probe = subprocess.run(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        raise RuntimeError(probe.stderr[-1000:])
    metadata = json.loads(probe.stdout)
    stream_types = {stream["codec_type"] for stream in metadata["streams"]}
    if not {"video", "audio"}.issubset(stream_types):
        raise RuntimeError("FFmpeg output is missing video or audio stream")
    return float(metadata["format"]["duration"])
