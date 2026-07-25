"""FFmpeg media processing (spec section 9 — final render).

- Still images become video segments via a subtle Ken Burns pan/zoom at the
  scene's exact audio duration.
- Generated clips are used as-is, trimmed/padded to match the audio.
- Segments are concatenated, `full_narration.mp3` is muxed in, and word-synced
  captions are burned from the merged timestamp timeline (unless the project
  has subtitles switched off).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import SUBTITLE_POSITION_DEFAULT, clamp_subtitle_position

W, H, FPS = 1080, 1920, 30
VOICE_FADE_IN_SECONDS = 1.8
VOICE_FADE_OUT_SECONDS = 2.4
MUSIC_FADE_IN_SECONDS = 4.5
MUSIC_FADE_OUT_SECONDS = 5.0
MUSIC_VOLUME = 0.075
FADE_CURVE = "qsin"


class FFmpegNotAvailable(RuntimeError):
    pass


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _run(args: list[str]) -> None:
    if not has_ffmpeg():
        raise FFmpegNotAvailable("ffmpeg is not installed or not on PATH")
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed:\n" + proc.stderr.decode(errors="replace")[-2000:]
        )


def _probe_duration(path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        return float(proc.stdout.decode().strip())
    except ValueError:
        return 0.0


def _fade_bounds(duration: float, fade_in: float, fade_out: float) -> tuple[float, float, float]:
    """Clamp fade durations so short videos do not produce invalid filters."""
    duration = max(0.0, duration)
    if duration <= 0:
        return 0.0, 0.0, 0.0
    fade_in = min(max(0.0, fade_in), duration / 2)
    fade_out = min(max(0.0, fade_out), max(0.0, duration - fade_in))
    fade_out_start = max(0.0, duration - fade_out)
    return fade_in, fade_out_start, fade_out


# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #
def ken_burns_clip(image_path: Path, out_path: Path, duration: float) -> Path:
    """Still -> video segment with a slow zoom, sized to WxH at the given length."""
    frames = max(1, int(round(duration * FPS)))
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
        f"crop={W * 2}:{H * 2},"
        f"zoompan=z='min(zoom+0.0006,1.15)':d={frames}:s={W}x{H}:fps={FPS},"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-t", f"{duration:.3f}", "-vf", vf, "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", str(out_path),
    ])
    return out_path


def _normalize_clip(clip_path: Path, out_path: Path, duration: float) -> Path:
    """Existing motion clip -> WxH, trimmed or frozen on its final frame."""
    clip_duration = _probe_duration(clip_path)
    pad = max(0.0, duration - clip_duration) if clip_duration > 0 else duration
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration={pad:.3f},"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-i", str(clip_path),
        "-t", f"{duration:.3f}", "-an", "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", str(out_path),
    ])
    return out_path


def concat_audio(scene_audio: list[Path], out_mp3: Path) -> Path:
    """Concatenate per-scene narration into full_narration.mp3."""
    existing = [p for p in scene_audio if p and Path(p).exists() and Path(p).stat().st_size > 0]
    if not existing:
        out_mp3.write_bytes(b"")
        return out_mp3
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in existing:
            f.write(f"file '{Path(p).resolve().as_posix()}'\n")
        listfile = f.name
    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
        "-c:a", "libmp3lame", "-q:a", "4", str(out_mp3),
    ])
    Path(listfile).unlink(missing_ok=True)
    return out_mp3


def merge_timestamps(scene_timestamp_files: list[Path], durations: list[float]) -> dict:
    """Merge per-scene word timings into one global timeline with absolute times."""
    global_words: list[dict] = []
    offset = 0.0
    for tsfile, dur in zip(scene_timestamp_files, durations):
        if tsfile and Path(tsfile).exists():
            try:
                data = json.loads(Path(tsfile).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {"words": []}
            for w in data.get("words", []):
                global_words.append({
                    "word": w["word"],
                    "start": round(offset + (w.get("start") or 0.0), 3),
                    "end": round(offset + (w.get("end") or 0.0), 3),
                })
        offset += dur
    return {"words": global_words, "duration": round(offset, 3)}


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def _fmt_ass_time(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\n", " ")


def _caption_lines(words: list[dict], max_line_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        token = str(word.get("word", "")).strip()
        candidate = f"{current} {token}".strip()
        if current and len(candidate) > max_line_chars:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _caption_text(words: list[dict], max_line_chars: int) -> str:
    text = " ".join(str(w.get("word", "")) for w in words).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    cleaned = [{"word": w} for w in text.split()]
    return r"\N".join(_ass_escape(line) for line in _caption_lines(cleaned, max_line_chars))


def _ends_sentence(word: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]]*$", word.strip()))


def _soft_break(word: str) -> bool:
    return bool(re.search(r"[,;:][\"')\]]*$", word.strip()))


def build_ass_captions(
    timeline: dict,
    out_path: Path,
    max_words: int = 10,
    max_span: float = 4.2,
    max_line_chars: int = 24,
    max_lines: int = 2,
    position: float = SUBTITLE_POSITION_DEFAULT,
) -> Path:
    """Group word timings into sentence-aware caption events and write ASS.

    `position` places the caption block vertically as a fraction of the frame
    height from the bottom edge; with the bottom-centre alignment used here it
    maps straight onto the style's MarginV.
    """
    margin_v = int(round(clamp_subtitle_position(position) * H))
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Arial,78,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,"
        "-1,0,0,0,100,100,0,0,1,7,3,2,96,96,%d,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (W, H, margin_v)

    lines: list[str] = []
    words = timeline.get("words", [])
    i = 0
    while i < len(words):
        group = [words[i]]
        j = i + 1
        while j < len(words):
            prev = group[-1]
            if _ends_sentence(str(prev.get("word", ""))):
                break
            span_if_added = words[j]["end"] - group[0]["start"]
            lines_if_added = _caption_lines([*group, words[j]], max_line_chars)
            too_long = len(group) >= max_words or span_if_added > max_span
            too_wide = len(lines_if_added) > max_lines
            if too_wide:
                break
            if too_long and _soft_break(str(prev.get("word", ""))):
                break
            if len(group) >= max_words + 4 or span_if_added > max_span + 1.8:
                break
            group.append(words[j])
            j += 1
        text = _caption_text(group, max_line_chars)
        start = _fmt_ass_time(group[0]["start"])
        end = _fmt_ass_time(group[-1]["end"])
        lines.append(f"Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}")
        i = j
    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Final render
# --------------------------------------------------------------------------- #
def render_final(
    segments: list[Path],
    narration_mp3: Path,
    captions_ass: Path | None,
    out_path: Path,
    music_path: Path | None = None,
    music_volume: float = MUSIC_VOLUME,
) -> Path:
    """Concat visual segments, mux narration, burn captions."""
    if not segments:
        raise RuntimeError("No scene segments to render.")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        list_file = tmp_path / "segments.txt"
        list_file.write_text(
            "".join(f"file '{Path(s).resolve().as_posix()}'\n" for s in segments),
            encoding="utf-8",
        )
        silent_video = tmp_path / "video.mp4"
        _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-r", str(FPS), str(silent_video),
        ])

        video_duration = _probe_duration(silent_video)
        if video_duration <= 0:
            video_duration = sum(max(0.0, _probe_duration(s)) for s in segments)

        has_music = music_path and Path(music_path).exists() and Path(music_path).stat().st_size > 0

        args = ["ffmpeg", "-y", "-i", str(silent_video)]
        has_audio = narration_mp3 and Path(narration_mp3).exists() and Path(narration_mp3).stat().st_size > 0
        if has_audio:
            args += ["-i", str(narration_mp3)]
        if has_music:
            args += ["-stream_loop", "-1", "-i", str(music_path)]

        if captions_ass and Path(captions_ass).exists():
            escaped = str(captions_ass).replace("\\", "/").replace(":", "\\:")
            args += ["-vf", f"ass='{escaped}'"]

        filters: list[str] = []
        audio_labels: list[str] = []
        if has_audio:
            voice_fade_in, voice_fade_out_start, voice_fade_out = _fade_bounds(
                video_duration,
                VOICE_FADE_IN_SECONDS,
                VOICE_FADE_OUT_SECONDS,
            )
            filters.append(
                "[1:a]aresample=44100,"
                f"afade=t=in:st=0:d={voice_fade_in:.3f}:curve={FADE_CURVE},"
                f"afade=t=out:st={voice_fade_out_start:.3f}:d={voice_fade_out:.3f}:curve={FADE_CURVE}"
                "[narr]"
            )
            audio_labels.append("[narr]")
        if has_music:
            music_input = 2 if has_audio else 1
            music_fade_in, music_fade_out_start, music_fade_out = _fade_bounds(
                video_duration,
                MUSIC_FADE_IN_SECONDS,
                MUSIC_FADE_OUT_SECONDS,
            )
            filters.append(
                f"[{music_input}:a]aresample=44100,"
                f"atrim=0:{video_duration:.3f},asetpts=PTS-STARTPTS,"
                f"volume={max(0.0, min(music_volume, 0.3)):.3f},"
                f"afade=t=in:st=0:d={music_fade_in:.3f}:curve={FADE_CURVE},"
                f"afade=t=out:st={music_fade_out_start:.3f}:d={music_fade_out:.3f}:curve={FADE_CURVE}"
                "[music]"
            )
            audio_labels.append("[music]")

        if len(audio_labels) > 1:
            filters.append(
                "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=first:dropout_transition=0:normalize=0[aout]"
            )
        elif len(audio_labels) == 1:
            filters.append(f"{audio_labels[0]}anull[aout]")

        if filters:
            args += ["-filter_complex", ";".join(filters)]
        args += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if filters:
            args += ["-c:a", "aac", "-map", "0:v:0", "-map", "[aout]", "-shortest"]
        args += [str(out_path)]
        _run(args)
    return out_path
