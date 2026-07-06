"""FFmpeg media processing (spec section 9 — final render).

- Still images become video segments via a subtle Ken Burns pan/zoom at the
  scene's exact audio duration.
- Generated clips are used as-is, trimmed/padded to match the audio.
- Segments are concatenated, `full_narration.mp3` is muxed in, and word-synced
  captions are burned from the merged timestamp timeline.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

W, H, FPS = 1080, 1920, 30


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
    """Existing motion clip -> WxH, trimmed/looped to exactly `duration`."""
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={FPS},format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip_path),
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


def build_ass_captions(timeline: dict, out_path: Path, max_words: int = 6, max_span: float = 3.2) -> Path:
    """Group words into short synced caption lines and write an .ass file."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        "Style: Caption,Arial,64,&H00FFFFFF,&H00000000,&H80000000,-1,0,1,4,1,2,80,80,220\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (W, H)

    lines: list[str] = []
    words = timeline.get("words", [])
    i = 0
    while i < len(words):
        group = [words[i]]
        j = i + 1
        while (
            j < len(words)
            and len(group) < max_words
            and (words[j]["end"] - group[0]["start"]) <= max_span
        ):
            group.append(words[j])
            j += 1
        text = " ".join(w["word"] for w in group)
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

        args = ["ffmpeg", "-y", "-i", str(silent_video)]
        has_audio = narration_mp3 and Path(narration_mp3).exists() and Path(narration_mp3).stat().st_size > 0
        if has_audio:
            args += ["-i", str(narration_mp3)]

        if captions_ass and Path(captions_ass).exists():
            escaped = str(captions_ass).replace("\\", "/").replace(":", "\\:")
            args += ["-vf", f"ass='{escaped}'"]

        args += ["-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if has_audio:
            args += ["-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        args += [str(out_path)]
        _run(args)
    return out_path
