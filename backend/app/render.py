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


def probe_audio(path: Path) -> tuple[float, str]:
    probe = subprocess.run(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        raise RuntimeError("uploaded file could not be read as audio")
    metadata = json.loads(probe.stdout)
    streams = metadata.get("streams", [])
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        raise RuntimeError("uploaded file does not contain an audio stream")
    if any(stream.get("codec_type") == "video" for stream in streams):
        raise RuntimeError("uploaded audio must not contain a video stream")
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("audio duration is invalid")
    format_name = str(metadata.get("format", {}).get("format_name", "")).split(",")[0]
    allowed = {"wav", "mp3", "ogg", "mov"}
    if format_name not in allowed:
        raise RuntimeError("voiceover format must be WAV, MP3, OGG, M4A, or MP4 audio")
    return duration, format_name


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


def compose_tour_video(
    shots: list[dict[str, Any]],
    output_path: Path,
    music_path: Path | None,
    opening_title: str = "",
    closing_title: str = "",
) -> float:
    if not shots:
        raise ValueError("at least one tour shot is required")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text_files: list[Path] = []
    try:
        inputs: list[str] = []
        for shot in shots:
            inputs += ["-i", str(shot["variant_path"]), "-i", str(shot["voiceover_path"])]
        music_index = len(shots) * 2
        if music_path:
            inputs += ["-stream_loop", "-1", "-i", str(music_path)]
        filters: list[str] = []
        video_labels: list[str] = []
        voice_labels: list[str] = []
        for index, shot in enumerate(shots):
            duration = float(shot["duration"])
            video_filter = (
                f"[{index * 2}:v:0]scale=720:1280:force_original_aspect_ratio=decrease,"
                "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=#111517,setsar=1,fps=25,"
                f"tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},setpts=PTS-STARTPTS"
            )
            overlays: list[tuple[str, str]] = []
            if index == 0 and opening_title:
                overlays.append((opening_title, f"between(t,0,{min(3.0, duration)})"))
            if index == len(shots) - 1 and closing_title:
                start = max(0.0, duration - min(3.0, duration))
                overlays.append((closing_title, f"between(t,{start},{duration})"))
            for text, enabled in overlays:
                wrapped = "\n".join(textwrap.wrap(text, width=24))
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="tour-title-",
                    suffix=".txt",
                    dir=output_path.parent,
                    delete=False,
                ) as target:
                    target.write(wrapped)
                    text_file = Path(target.name)
                text_files.append(text_file)
                escaped = (
                    str(text_file).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                )
                video_filter += (
                    f",drawbox=x=40:y=ih*0.70:w=iw-80:h=220:color=black@0.62:t=fill:enable='{enabled}',"
                    f"drawtext=textfile='{escaped}':expansion=none:fontcolor=white:fontsize=48:"
                    f"line_spacing=14:x=(w-text_w)/2:y=h*0.70+(220-text_h)/2:enable='{enabled}'"
                )
            video_label = f"tv{index}"
            filters.append(f"{video_filter}[{video_label}]")
            video_labels.append(f"[{video_label}]")
            voice_label = f"ta{index}"
            filters.append(
                f"[{index * 2 + 1}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=mono,"
                f"apad,atrim=duration={duration},"
                f"asetpts=PTS-STARTPTS[{voice_label}]"
            )
            voice_labels.append(f"[{voice_label}]")
        filters.append(f"{''.join(video_labels)}concat=n={len(shots)}:v=1:a=0[tourv]")
        filters.append(
            f"{''.join(voice_labels)}concat=n={len(shots)}:v=0:a=1,"
            "aformat=sample_fmts=fltp:channel_layouts=mono[tourvoice]"
        )
        total_duration = sum(float(shot["duration"]) for shot in shots)
        if music_path:
            filters += [
                (
                    f"[{music_index}:a:0]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=mono,"
                    f"volume=0.22,atrim=duration={total_duration},asetpts=PTS-STARTPTS[music]"
                ),
                "[tourvoice]asplit=2[voiceout][sidechain]",
                "[music][sidechain]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=350[ducked]",
                "[ducked][voiceout]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.95[toura]",
            ]
        else:
            filters.append("[tourvoice]alimiter=limit=0.95[toura]")
        command = [
            settings.ffmpeg_executable,
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[tourv]",
            "-map",
            "[toura]",
            "-t",
            str(total_duration),
            "-c:v",
            "libx264",
            "-r",
            "25",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    finally:
        for text_file in text_files:
            text_file.unlink(missing_ok=True)
    if completed.returncode:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(completed.stderr[-1000:])
    duration = probe_duration(output_path)
    if abs(duration - total_duration) > 0.15:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("tour render duration does not match approved timeline")
    probe = subprocess.run(
        [
            settings.ffprobe_executable,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("tour render output could not be inspected")
    streams = {
        stream.get("codec_type"): stream for stream in json.loads(probe.stdout).get("streams", [])
    }
    video, audio = streams.get("video"), streams.get("audio")
    if (
        not video
        or video.get("codec_name") != "h264"
        or (video.get("width"), video.get("height")) != (720, 1280)
        or not audio
        or audio.get("codec_name") != "aac"
    ):
        output_path.unlink(missing_ok=True)
        raise RuntimeError("tour render output does not match the required media profile")
    return duration
