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
import functools
import tempfile
from pathlib import Path

from PIL import ImageFont

from ..config import SUBTITLE_POSITION_DEFAULT, clamp_subtitle_position
from .typography import DASHES, with_comma

W, H, FPS = 1080, 1920, 30
VOICE_FADE_IN_SECONDS = 1.8
VOICE_FADE_OUT_SECONDS = 2.4
MUSIC_FADE_IN_SECONDS = 4.5
MUSIC_FADE_OUT_SECONDS = 5.0
MUSIC_VOLUME = 0.075
FADE_CURVE = "qsin"

# Caption body size, as a share of the 1920-tall frame. The old 78px was 4% of
# frame height, which is legible on a monitor and small on a phone held at
# arm's length in a feed. Side margins come in to pay for it so a line still
# fits without wrapping to three.
CAPTION_FONT_SIZE = 96
CAPTION_SIDE_MARGIN = 60
CAPTION_MAX_LINE_CHARS = 17

# The opening hook, burned over the top of the frame while the narration's own
# captions run underneath. It is the one piece of text a scrolling viewer reads
# before deciding, so it is built as a card rather than a larger caption: an
# amber kicker naming the arc, then the claim itself in heavy display type.
HOOK_FONT_NAME = "Arial Black"
HOOK_FONT_FILE = "ariblk.ttf"  # the same face, for measuring (see _hook_lines)
# Three seconds, not two: at two the card is gone before a viewer who arrived
# mid-scroll has finished reading it, which is the one job it has.
HOOK_HOLD_SECONDS = 3.0
HOOK_TOP_MARGIN = 0.089  # fraction of frame height, from the top edge
HOOK_SIDE_MARGIN = 16
HOOK_KICKER_SIZE = 76
HOOK_KICKER_SPACING = 6  # letter-spacing, which is what makes a kicker a kicker
HOOK_KICKER_GAP = 14
# ASS has no line-spacing control, so each line is positioned individually and
# this is the step between them, as a share of the font size. Below ~0.8 the
# descenders of one line start touching the caps of the next.
HOOK_LEADING_RATIO = 0.86
# The statement is sized to the frame rather than fixed: the largest size at
# which it still fits HOOK_MAX_LINES. A short hook gets bigger type and a long
# one steps down, so the card fills the same space whatever the model writes.
HOOK_MAX_LINES = 2
HOOK_SIZE_MIN = 64
HOOK_SIZE_MAX = 132


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
# How far a push/pull travels, and the fixed zoom a pan or tilt sits at so there
# is off-screen image left to travel across. 1.15 gives ~13% of the frame to
# move through: clearly a camera move, never so much that the still goes soft.
KEN_BURNS_ZOOM = 1.15
# A pan or tilt starts a little tighter than it ends, so the frame creeps in
# while it travels instead of sliding at a fixed scale.
PAN_ZOOM_START, PAN_ZOOM_TRAVEL = 1.12, 0.08
# How far the impact move drives in. Bigger than a push on purpose — it is
# meant to land, not to drift.
PUNCH_ZOOM = 1.30

# How far a generated clip may be slowed to cover its scene before the rest is
# held on the final frame instead. Past roughly a third slower, gestures start
# reading as sludge rather than as deliberate pacing.
_MAX_STRETCH = 1.35


def _ken_burns_expressions(move: str, frames: int) -> tuple[str, str, str]:
    """(zoom, x, y) zoompan expressions for one camera move.

    ``on`` is the output frame index, so the ramp goes 0 -> 1 across the segment
    and every move lands exactly on its end point regardless of length.

    Two rules constrain how these are written. No expression may contain a
    comma, because zoompan is one filter in a comma-separated chain — which
    rules out ``min()``, ``pow()`` and ``if()``, so every curve below is built
    from plain multiplication. And the ramps are eased rather than linear: a
    constant-speed camera move is the thing that reads as "slideshow", while
    the same move with acceleration and settle reads as a decision.
    """
    span = max(1, frames - 1)
    t = f"(on/{span})"
    # Smoothstep: starts slow, accelerates, settles. The default for travel.
    ease = f"({t}*{t}*(3-2*{t}))"
    # Quartic ease-out: almost all of the movement happens immediately, then it
    # holds. This is the impact curve, not a camera drift.
    snap = f"(1-(1-{t})*(1-{t})*(1-{t})*(1-{t}))"
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    # Full width/height left over at this zoom — how far a pan or tilt can go.
    span_x, span_y = "(iw-iw/zoom)", "(ih-ih/zoom)"
    travel = KEN_BURNS_ZOOM - 1.0
    z = f"{KEN_BURNS_ZOOM:.4f}"
    # Pans and tilts creep in slightly while they travel. A sideways move at a
    # locked zoom looks like a photo on a slider; the drift sells it as a camera.
    pan_z = f"{PAN_ZOOM_START:.4f}+{PAN_ZOOM_TRAVEL:.4f}*{ease}"
    return {
        # Chosen when stillness is the point, so it really does hold.
        "static": ("1", "0", "0"),
        "push_in": (f"1+{travel:.4f}*{ease}", center_x, center_y),
        "pull_out": (f"{z}-{travel:.4f}*{ease}", center_x, center_y),
        "pan_left": (pan_z, f"{span_x}*(1-{ease})", center_y),
        "pan_right": (pan_z, f"{span_x}*{ease}", center_y),
        "tilt_up": (pan_z, center_x, f"{span_y}*(1-{ease})"),
        "tilt_down": (pan_z, center_x, f"{span_y}*{ease}"),
        "punch_in": (f"1+{PUNCH_ZOOM - 1.0:.4f}*{snap}", center_x, center_y),
    }.get(move, (f"1+{travel:.4f}*{ease}", center_x, center_y))


def ken_burns_clip(
    image_path: Path, out_path: Path, duration: float, move: str = "push_in"
) -> Path:
    """Still -> video segment with a real camera move, at the scene's duration.

    ``move`` is a ``models.CameraMove`` value. On a still-heavy video this is the
    only motion the viewer sees, so it is chosen per scene rather than fixed —
    ten scenes sharing one slow zoom is what makes a storyboard read as a
    slideshow.

    Panels are rendered by services.parallax instead; this is the fallback for
    when that fails, and the stand-in used by the offline adapters. zoompan can
    only crop at whole input pixels, so a slow eased move stutters here — which
    is exactly why the panel renderer does its own sub-pixel sampling.
    """
    frames = max(1, int(round(duration * FPS)))
    z, x, y = _ken_burns_expressions(str(move), frames)
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
        f"crop={W * 2}:{H * 2},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{H}:fps={FPS},"
        f"format=yuv420p"
    )
    _run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-t", f"{duration:.3f}", "-vf", vf, "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", str(out_path),
    ])
    return out_path


def _normalize_clip(clip_path: Path, out_path: Path, duration: float) -> Path:
    """Existing motion clip -> WxH, covering the scene's full audio duration.

    A generated clip is often shorter than its scene, because generation is
    billed by the second and the back half of a long clip is where an i2v model
    drifts off the art direction. The gap is closed in the order that looks the
    least like a mistake: a modest slowdown first, which keeps real motion
    running for longer, and a held final frame only for whatever is still
    missing past ``_MAX_STRETCH``.
    """
    clip_duration = _probe_duration(clip_path)
    if clip_duration <= 0:
        clip_duration = duration
    stretch = duration / clip_duration if clip_duration > 0 else 1.0
    # Slow to the cap (1.0 = untouched), then hold whatever is still missing.
    speed = min(max(stretch, 1.0), _MAX_STRETCH)
    pad = max(0.0, duration - clip_duration * speed)
    # setpts must come BEFORE fps: stretching timestamps after the frame rate is
    # fixed leaves the segment at a fraction of FPS, and every segment has to
    # share one cadence for the concat to hold sync.
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},"
        f"setpts={speed:.5f}*PTS,"
        f"fps={FPS},"
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


@functools.lru_cache(maxsize=None)
def _hook_measurer(size: int):
    """A width function for the hook face at ``size``, in pixels.

    Measured from the real glyph advances rather than counted in characters.
    Character counts have to be set conservatively because being wrong means
    libass silently re-wrapping a line, which turns a balanced two-line hook
    into a ragged three; measuring lets the margins go out to the frame edge.
    Falls back to an average-advance estimate where the face is not installed,
    which keeps a Linux render legible rather than exact.

    The result is cached per size, and memoises its own calls: the search below
    asks for the same substrings repeatedly, and the measurement is the only
    expensive part of it.
    """
    try:
        font = ImageFont.truetype(HOOK_FONT_FILE, size)
    except OSError:
        return functools.lru_cache(maxsize=None)(
            lambda text: int(len(text) * size * 0.62)
        )

    @functools.lru_cache(maxsize=None)
    def width(text: str) -> int:
        box = font.getbbox(text)
        return box[2] - box[0]

    return width


def _pack_lines(words: tuple[str, ...], limit: int, usable: int, width) -> list[str] | None:
    """Greedy wrap at ``limit`` px, or None if a line would exceed ``usable``."""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and width(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
        if width(current) > usable:
            return None
    if current:
        lines.append(current)
    return lines


def _balanced_lines(words: tuple[str, ...], usable: int, width) -> list[str] | None:
    """The fewest lines that fit, split as evenly as the words allow.

    A greedy wrap fills each line to the limit and leaves whatever is left on
    the last one, which routinely strands a single word. Keeping the greedy
    line count and then finding the narrowest limit that still reaches it gives
    the evenest split available without breaking a word.

    Only the widths of contiguous runs of words can change where a greedy wrap
    breaks, so those are the limits worth trying — a few dozen, rather than
    every pixel between the longest word and the frame.
    """
    greedy = _pack_lines(words, usable, usable, width)
    if greedy is None:
        return None
    candidates = {
        width(" ".join(words[start:stop]))
        for start in range(len(words))
        for stop in range(start + 1, len(words) + 1)
    }
    for limit in sorted(c for c in candidates if c <= usable):
        balanced = _pack_lines(words, limit, usable, width)
        if balanced is not None and len(balanced) == len(greedy):
            return balanced
    return greedy


def _hook_lines(hook: str) -> tuple[int, list[str]]:
    """(font size, wrapped lines) for the hook, sized to fill the frame width.

    Line count only ever grows with font size, so the largest size that still
    fits ``HOOK_MAX_LINES`` is found by bisection rather than by trying each.
    """
    words = tuple(hook.split())
    usable = W - 2 * HOOK_SIDE_MARGIN

    def wrap_at(size: int) -> list[str] | None:
        return _balanced_lines(words, usable, _hook_measurer(size))

    best = wrap_at(HOOK_SIZE_MIN)
    if best is None:  # a single word too wide even at the floor; let ASS cope
        return HOOK_SIZE_MIN, [hook]
    # Normally HOOK_MAX_LINES. A hook written far longer than the prompt asks
    # for gets the extra lines it needs instead of one unwrapped line, which
    # libass would break at its own width and undo the balancing above.
    budget = max(HOOK_MAX_LINES, len(best))

    low, high, size = HOOK_SIZE_MIN, HOOK_SIZE_MAX, HOOK_SIZE_MIN
    while low <= high:
        mid = (low + high) // 2
        lines = wrap_at(mid)
        if lines is None or len(lines) > budget:
            high = mid - 1
        else:
            size, best, low = mid, lines, mid + 1
    return size, best


def _caption_text(words: list[dict], max_line_chars: int) -> str:
    text = " ".join(str(w.get("word", "")) for w in words).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    cleaned = [{"word": w} for w in text.split()]
    return r"\N".join(_ass_escape(line) for line in _caption_lines(cleaned, max_line_chars))


def _dashless_words(words: list[dict]) -> list[dict]:
    """Caption tokens with dashes-as-punctuation rewritten as commas.

    The word-timing form of services.typography, which explains why. Tokens
    arrive already split by the aligner, so the work here is deciding which
    neighbour a mark belongs to rather than parsing prose: a hyphen INSIDE a
    token is spelling and stays, and only leading and trailing ones are marks.

    Timing survives: a token that was nothing but a dash hands its span to the
    neighbour that absorbs it, so every caption after it still lands on the word
    it belongs to.
    """
    out: list[dict] = []
    pending_start: float | None = None
    for word in words:
        token = str(word.get("word", "")).strip()
        core = token.strip(DASHES)
        if not core:
            # A dash standing on its own as a word. It leaves no text behind,
            # so its span goes to whichever neighbour is available.
            if out:
                out[-1]["end"] = word.get("end", out[-1]["end"])
                out[-1]["word"] = with_comma(out[-1]["word"])
            elif pending_start is None:
                pending_start = word.get("start")
            continue
        if out and token[0] in DASHES:
            out[-1]["word"] = with_comma(out[-1]["word"])
        if token[-1] in DASHES:
            core = with_comma(core)
        entry = {**word, "word": core}
        if pending_start is not None:
            entry["start"] = pending_start
            pending_start = None
        out.append(entry)
    return out


def _ends_sentence(word: str) -> bool:
    return bool(re.search(r"[.!?][\"')\]]*$", word.strip()))


def _soft_break(word: str) -> bool:
    return bool(re.search(r"[,;:][\"')\]]*$", word.strip()))


def build_ass_captions(
    timeline: dict,
    out_path: Path,
    max_words: int = 10,
    max_span: float = 2.6,
    max_line_chars: int = CAPTION_MAX_LINE_CHARS,
    max_lines: int = 2,
    position: float = SUBTITLE_POSITION_DEFAULT,
    hook_text: str = "",
    hook_kicker: str = "",
) -> Path:
    """Group word timings into sentence-aware caption events and write ASS.

    `position` places the caption block vertically as a fraction of the frame
    height from the bottom edge; with the bottom-centre alignment used here it
    maps straight onto the style's MarginV.

    `hook_text`, when given, is burned across the top of the frame for the first
    ``HOOK_HOLD_SECONDS``, under an optional `hook_kicker` naming the arc. Both
    run on their own styles and their own alignment, so the card sits above the
    narration captions rather than displacing them.
    """
    margin_v = int(round(clamp_subtitle_position(position) * H))
    hook_top = int(round(HOOK_TOP_MARGIN * H))
    hook = " ".join((hook_text or "").split())
    kicker = " ".join((hook_kicker or "").split()).upper()
    hook_size, hook_lines = _hook_lines(hook) if hook else (HOOK_SIZE_MIN, [])
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Arial,%d,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,"
        "-1,0,0,0,100,100,0,0,1,9,4,2,%d,%d,%d,1\n"
        # Alignment 8 is top-centre on both hook styles, and each line is placed
        # with an explicit \\pos, so their MarginV is unused. Amber is written
        # &HAABBGGRR, hence the reversed-looking constant.
        "Style: Kick,%s,%d,&H0024B2FF,&H000000FF,&H00000000,&HA0000000,"
        "0,0,0,0,100,100,%d,0,1,6,2,8,%d,%d,0,1\n"
        "Style: Hook,%s,%d,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,"
        "0,0,0,0,100,100,0,0,1,9,4,8,%d,%d,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (
        W, H,
        CAPTION_FONT_SIZE, CAPTION_SIDE_MARGIN, CAPTION_SIDE_MARGIN, margin_v,
        HOOK_FONT_NAME, HOOK_KICKER_SIZE, HOOK_KICKER_SPACING,
        HOOK_SIDE_MARGIN, HOOK_SIDE_MARGIN,
        HOOK_FONT_NAME, hook_size, HOOK_SIDE_MARGIN, HOOK_SIDE_MARGIN,
    )

    lines: list[str] = []
    if hook:
        # Layer 1 so the card draws over a caption that reaches up this far.
        start, end = _fmt_ass_time(0.0), _fmt_ass_time(HOOK_HOLD_SECONDS)
        y = hook_top
        if kicker:
            lines.append(
                "Dialogue: 1,%s,%s,Kick,,0,0,0,,{\\an8\\pos(%d,%d)}%s"
                % (start, end, W // 2, y, _ass_escape(kicker))
            )
            y += HOOK_KICKER_SIZE + HOOK_KICKER_GAP
        leading = round(hook_size * HOOK_LEADING_RATIO)
        for index, line in enumerate(hook_lines):
            lines.append(
                "Dialogue: 1,%s,%s,Hook,,0,0,0,,{\\an8\\pos(%d,%d)}%s"
                % (start, end, W // 2, y + index * leading, _ass_escape(line))
            )
    # Before grouping, not after: the comma a dash becomes is also a legitimate
    # place to break a caption, and the grouping below reads for exactly that.
    words = _dashless_words(timeline.get("words", []))
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
