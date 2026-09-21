import json
import math
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

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
    intro_card: dict[str, Any] | None = None,
    outro_card: dict[str, Any] | None = None,
) -> float:
    if not input_paths:
        raise ValueError("at least one video input is required")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    card_files: list[Path] = []
    try:
        inputs: list[str] = []
        if not intro_card and not outro_card:
            for source_path in input_paths:
                inputs += ["-i", str(source_path)]
            video_inputs = "".join(f"[{index}:v:0]" for index in range(len(input_paths)))
            filters = [f"{video_inputs}concat=n={len(input_paths)}:v=1:a=0[v]"]
            audio_index = len(input_paths)
        else:
            segments: list[tuple[Path | None, dict[str, Any] | None, str | None]] = []
            if intro_card:
                artist = "\n".join(textwrap.wrap(str(intro_card["artist_name"]), width=32))
                title = "\n".join(textwrap.wrap(str(intro_card["song_title"]), width=32))
                segments.append((None, intro_card, f"{artist}\n{title}"))
            for source_path in input_paths:
                segments.append((source_path, None, None))
            if outro_card:
                outro_text = "\n".join(textwrap.wrap(str(outro_card["text"]), width=32))
                segments.append((None, outro_card, outro_text))
            card_by_index: dict[int, Path] = {}
            for index, (path, card, text) in enumerate(segments):
                if path:
                    inputs += ["-i", str(path)]
                    continue
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="render-card-",
                    suffix=".txt",
                    dir=output_path.parent,
                    delete=False,
                ) as text_output:
                    text_output.write(text or "")
                    text_file = Path(text_output.name)
                card_files.append(text_file)
                card_by_index[index] = text_file
                inputs += [
                    "-f",
                    "lavfi",
                    "-t",
                    str(card["duration_seconds"]),
                    "-i",
                    "color=c=#111517:s=1280x720:r=25",
                ]
            filters = []
            total_video_inputs = len(segments)
            for index in range(total_video_inputs):
                normalized = (
                    f"[{index}:v:0]scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#111517,"
                    "setsar=1,fps=25,format=yuv420p"
                )
                if index in card_by_index:
                    escaped = (
                        str(card_by_index[index])
                        .replace("\\", "\\\\")
                        .replace(":", "\\:")
                        .replace("'", "\\'")
                    )
                    normalized += (
                        f",drawtext=textfile='{escaped}':expansion=none:fontcolor=white:fontsize=54:"
                        "line_spacing=18:x=(w-text_w)/2:y=(h-text_h)/2"
                    )
                filters.append(f"{normalized}[v{index}]")
            video_inputs = "".join(f"[v{index}]" for index in range(total_video_inputs))
            filters.append(f"{video_inputs}concat=n={total_video_inputs}:v=1:a=0[v]")
            audio_index = total_video_inputs
        inputs += ["-ss", str(audio_start_seconds), "-i", str(audio_path)]
        command = [
            settings.ffmpeg_executable,
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-map",
            f"{audio_index}:a:0",
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
    finally:
        for text_file in card_files:
            text_file.unlink(missing_ok=True)
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
