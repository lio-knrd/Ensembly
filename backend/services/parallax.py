"""Depth-based 2.5D parallax over a still panel.

A camera move applied uniformly to a flat image is honest but flat: everything
in the frame travels at the same rate, which is exactly what a photograph on a
slider looks like. Real depth means near things sweep past faster than far
things, and that difference is most of what sells a still as a shot.

The panel is warped continuously: every point is displaced by an amount that
depends on its own estimated depth, near points furthest. This is done as a
single mesh transform, so each source pixel lands in exactly one place.

An earlier version of this module composited discrete depth layers on top of an
un-masked copy of the panel. That duplicates content — when the near layer
moves, the original of that content is still underneath at its old position, so
a face or a pair of hands visibly doubles. A continuous warp cannot do that.
Its own failure mode is mild stretching across sharp depth edges, which reads
as depth of field rather than as a mistake.

Depth comes from Depth Anything V2 Small via onnxruntime. Both the runtime and
the weights are optional, exactly like the manim engine: ``available()`` is
false without them and callers fall back to the flat ken-burns path.
"""
from __future__ import annotations

import hashlib
import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from ..config import settings

W, H, FPS = 1080, 1920, 30

# --------------------------------------------------------------------------- #
# Particles
# --------------------------------------------------------------------------- #
# Each kind is (count, radius px, RGB, fall px/sec, sway px, sway hz, alpha).
# Counts are deliberately low: the point is atmosphere at the edge of notice,
# and anything dense enough to read as "an effect" fights the artwork.
_PARTICLE_KINDS: dict[str, tuple[int, tuple[int, int], tuple[int, int, int], float, float, float, int]] = {
    "dust":   (70,  (2, 5),  (235, 228, 210),  14.0, 26.0, 0.11, 120),
    "petals": (46,  (7, 15), (244, 198, 214),  92.0, 62.0, 0.28, 205),
    "embers": (56,  (3, 7),  (255, 168,  74), -78.0, 40.0, 0.34, 225),
    "snow":   (95,  (4, 9),  (245, 249, 255),  74.0, 46.0, 0.19, 195),
    "ash":    (76,  (3, 8),  (188, 184, 178),  54.0, 36.0, 0.16, 150),
}
# How long a transition takes, as a share of the incoming panel. Kept short:
# it is an arrival, not a scene of its own.
_TRANSITION_SECONDS = 0.32

# Depth Anything V2 Small. 518 is the resolution it was exported at; the model
# is fully convolutional but off-size inputs cost accuracy for no benefit here.
_MODEL_NAME = "depth_anything_v2_small.onnx"
_INPUT_SIZE = 518
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# How much of the camera move the furthest and nearest points take. The spread
# between them IS the effect; keeping it modest is what keeps the warp from
# tearing across depth edges.
_GAIN_FAR, _GAIN_NEAR = 0.86, 1.22
# Warp mesh resolution. Fine enough to follow a subject's outline, coarse enough
# that building the mesh is not itself the cost.
_MESH_COLS, _MESH_ROWS = 32, 56
# The depth map is smoothed before use: raw per-pixel depth on stylised art is
# noisy, and noise in a displacement field shows up as shimmer.
_DEPTH_BLUR = 6.0

_session = None


def model_path() -> Path:
    return Path(settings.projects_dir.parent.parent) / "data" / "models" / _MODEL_NAME


def available() -> bool:
    """Whether depth estimation can actually run right now."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return model_path().exists()


def _get_session():
    global _session
    if _session is None:
        import onnxruntime as ort

        # GPU when the user has the CUDA runtime installed, CPU otherwise. CPU
        # inference is well under a second per panel, so this is an optimisation
        # rather than a requirement.
        providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider")
                     if p in ort.get_available_providers()]
        _session = ort.InferenceSession(str(model_path()), providers=providers)
    return _session


def depth_map(image_path: Path, cache: bool = True) -> np.ndarray:
    """Normalised depth for a panel, 1.0 nearest and 0.0 furthest.

    Cached beside the image, because the same panel is re-rendered every time
    the project is re-rendered and the depth never changes.
    """
    cache_file = Path(image_path).with_suffix(".depth.npy")
    if cache and cache_file.exists():
        try:
            return np.load(cache_file)
        except (OSError, ValueError):
            pass

    session = _get_session()
    image = Image.open(image_path).convert("RGB").resize((_INPUT_SIZE, _INPUT_SIZE), Image.BILINEAR)
    x = np.asarray(image, dtype=np.float32) / 255.0
    x = ((x - _IMAGENET_MEAN) / _IMAGENET_STD).transpose(2, 0, 1)[None]
    raw = session.run(None, {session.get_inputs()[0].name: x})[0][0]

    lo, hi = float(raw.min()), float(raw.max())
    depth = (raw - lo) / (hi - lo) if hi > lo else np.zeros_like(raw)
    depth = depth.astype(np.float32)
    if cache:
        try:
            np.save(cache_file, depth)
        except OSError:
            pass
    return depth


def _mesh_depth(depth: np.ndarray) -> np.ndarray:
    """Depth sampled at the warp mesh's vertices, smoothed and rescaled 0..1."""
    smoothed = Image.fromarray((depth * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(_DEPTH_BLUR)
    ).resize((_MESH_COLS + 1, _MESH_ROWS + 1), Image.BILINEAR)
    d = np.asarray(smoothed, dtype=np.float32) / 255.0
    # Stretch to the full range so a panel whose depth all sits in a narrow band
    # still gets the whole parallax spread rather than none of it.
    lo, hi = float(d.min()), float(d.max())
    return (d - lo) / (hi - lo) if hi - lo > 1e-3 else np.full_like(d, 0.5)


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fully cover the target box, then centre-crop to it."""
    tw, th = size
    scale = max(tw / image.width, th / image.height)
    resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
    left, top = (resized.width - tw) // 2, (resized.height - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def _move_at(move: str, t: float) -> tuple[float, float, float]:
    """(zoom, x fraction, y fraction) for a camera move at eased time t.

    Mirrors services.ffmpeg._ken_burns_expressions so a panel looks the same
    whether or not depth was available — parallax changes how much each layer
    moves, never what the move is.
    """
    ease = t * t * (3 - 2 * t)
    snap = 1 - (1 - t) ** 4
    base, travel = 1.15, 0.15
    pan_z = 1.12 + 0.08 * ease
    return {
        "static": (1.0, 0.0, 0.0),
        "push_in": (1.0 + travel * ease, 0.0, 0.0),
        "pull_out": (base - travel * ease, 0.0, 0.0),
        "pan_left": (pan_z, 0.5 - ease, 0.0),
        "pan_right": (pan_z, ease - 0.5, 0.0),
        "tilt_up": (pan_z, 0.0, 0.5 - ease),
        "tilt_down": (pan_z, 0.0, ease - 0.5),
        "punch_in": (1.0 + 0.30 * snap, 0.0, 0.0),
    }.get(move, (1.0 + travel * ease, 0.0, 0.0))


def _particle_field(kind: str, seed: str, size: tuple[int, int]) -> list[dict]:
    """Lay out one panel's particles.

    Seeded from the panel identity so a re-render is pixel-identical to the
    cached one — otherwise the segment cache would hand back a clip that no
    longer matches what a fresh render produces.
    """
    spec = _PARTICLE_KINDS.get(kind)
    if not spec:
        return []
    count, (r_lo, r_hi), color, fall, sway, sway_hz, alpha = spec
    rng = random.Random(hashlib.sha1(f"{kind}:{seed}".encode()).hexdigest())
    width, height = size
    field = []
    for _ in range(count):
        radius = rng.uniform(r_lo, r_hi)
        # Bigger motes read as nearer, so they fall faster and fade less.
        nearness = (radius - r_lo) / max(1e-6, r_hi - r_lo)
        field.append({
            "x": rng.uniform(-0.05, 1.05) * width,
            "y": rng.uniform(-0.15, 1.15) * height,
            "r": radius,
            "color": color,
            "alpha": int(alpha * rng.uniform(0.45, 1.0)),
            "fall": fall * (0.65 + 0.7 * nearness),
            "sway": sway * rng.uniform(0.4, 1.3),
            "hz": sway_hz * rng.uniform(0.7, 1.4),
            "phase": rng.uniform(0, math.tau),
        })
    return field


def _draw_particles(field: list[dict], t: float, size: tuple[int, int]) -> Image.Image:
    """The particle layer at time ``t`` seconds.

    Screen space, drawn over the finished frame. Depth separation comes from
    size and speed — bigger, faster motes read as nearer — rather than from
    occluding the subject, which the continuous warp has no layers to do.
    """
    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for p in field:
        y = p["y"] + p["fall"] * t
        # Wrap with a margin so nothing pops in at the frame edge.
        span = height * 1.3
        y = ((y + height * 0.15) % span) - height * 0.15
        x = p["x"] + p["sway"] * math.sin(math.tau * p["hz"] * t + p["phase"])
        r = p["r"]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*p["color"], p["alpha"]))
    return layer


def _apply_transition(
    frame: Image.Image, prev: Image.Image, kind: str, progress: float
) -> Image.Image:
    """Blend the incoming panel over the previous panel's last frame.

    ``progress`` runs 0 -> 1 across the transition. This happens inside the
    incoming segment, so the segment's duration — and therefore every caption
    timing after it — is completely unaffected.
    """
    width, height = frame.size
    e = progress * progress * (3 - 2 * progress)
    if kind == "fade":
        return Image.blend(prev, frame, e)
    if kind == "flash":
        # Previous panel blown to white, then the new panel resolving out of it.
        if e < 0.5:
            return Image.blend(prev, Image.new("RGB", frame.size, (255, 255, 255)), e * 2)
        return Image.blend(Image.new("RGB", frame.size, (255, 255, 255)), frame, (e - 0.5) * 2)
    if kind in ("slide_up", "slide_left"):
        out = prev.copy()
        if kind == "slide_up":
            offset = int(round(height * (1 - e)))
            out.paste(prev.crop((0, 0, width, height)), (0, -int(round(height * e * 0.25))))
            out.paste(frame, (0, offset))
        else:
            offset = int(round(width * (1 - e)))
            out.paste(prev.crop((0, 0, width, height)), (-int(round(width * e * 0.25)), 0))
            out.paste(frame, (offset, 0))
        return out
    return frame


def parallax_clip(
    image_path: Path,
    out_path: Path,
    duration: float,
    move: str = "push_in",
    size: tuple[int, int] = (W, H),
    particles: str = "none",
    transition: str = "cut",
    prev_frame: Image.Image | None = None,
    use_depth: bool = True,
) -> Path:
    """Render one panel as a shot and encode it to ``out_path``.

    Three independent layers, any combination of which may be active: the camera
    move, depth parallax on top of it, and a particle overlay on top of that.
    ``transition`` blends the opening frames out of ``prev_frame`` — inside this
    panel's own duration, so the caption timeline never shifts.

    Raises if depth was asked for and is unavailable, so the caller can fall
    back rather than silently producing a flat clip that claims to be parallax.
    """
    depth_on = use_depth and available()
    if use_depth and not depth_on:
        raise RuntimeError("depth model unavailable")

    width, height = size
    frames = max(1, int(round(duration * FPS)))
    panel = _cover(Image.open(image_path).convert("RGB"), size)

    mesh_depth = _mesh_depth(depth_map(image_path)) if depth_on else None
    field = _particle_field(particles, str(image_path), size)
    transition_frames = (
        min(frames, max(1, int(round(_TRANSITION_SECONDS * FPS))))
        if prev_frame is not None and transition != "cut"
        else 0
    )

    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
         "-an", "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p",
         str(out_path)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        span = max(1, frames - 1)
        for i in range(frames):
            seconds = i / FPS
            zoom, fx, fy = _move_at(move, i / span)
            # Opaque RGB throughout, which is also the pixel format ffmpeg wants,
            # so the finished frame needs no conversion.
            if mesh_depth is not None:
                frame = _warp(panel, mesh_depth, zoom, fx, fy, size)
            else:
                frame = _place(panel, zoom, fx, fy, 1.0, size)
            if field:
                overlay = _draw_particles(field, seconds, size)
                frame.paste(overlay, (0, 0), overlay)
            if i < transition_frames:
                frame = _apply_transition(
                    frame, prev_frame, transition, (i + 1) / transition_frames
                )
            proc.stdin.write(frame.tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError("ffmpeg failed during panel encode:\n" + err[-2000:])
    return out_path


def last_frame(clip_path: Path, size: tuple[int, int] = (W, H)) -> Image.Image | None:
    """The final frame of a rendered segment, for the next panel to arrive from."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-sseof", "-0.2", "-i", str(clip_path),
         "-update", "1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    expected = size[0] * size[1] * 3
    if proc.returncode != 0 or len(proc.stdout) < expected:
        return None
    return Image.frombytes("RGB", size, proc.stdout[-expected:])


def _warp(panel: Image.Image, mesh_depth: np.ndarray, zoom: float, fx: float,
          fy: float, size: tuple[int, int]) -> Image.Image:
    """The panel under a depth-dependent camera move, as one mesh transform.

    Each mesh vertex is mapped by exactly the rule in ``_place``, but with its
    own gain taken from its own depth — so the mapping is continuous in depth
    and no part of the image is ever drawn twice.
    """
    width, height = size
    gains = _GAIN_FAR + (_GAIN_NEAR - _GAIN_FAR) * mesh_depth
    z = np.maximum(1.0, 1.0 + (zoom - 1.0) * gains)
    win_w, win_h = width / z, height / z
    slack_x, slack_y = width - win_w, height - win_h
    left = np.clip(slack_x / 2 + fx * gains * slack_x, 0.0, slack_x)
    top = np.clip(slack_y / 2 + fy * gains * slack_y, 0.0, slack_y)

    xs = np.linspace(0, width, _MESH_COLS + 1)
    ys = np.linspace(0, height, _MESH_ROWS + 1)
    # Source coordinate for every mesh vertex.
    src_x = xs[None, :] / z + left
    src_y = ys[:, None] / z + top

    mesh = []
    for r in range(_MESH_ROWS):
        for c in range(_MESH_COLS):
            box = (int(xs[c]), int(ys[r]), int(xs[c + 1]), int(ys[r + 1]))
            # PIL wants the source quad as NW, SW, SE, NE.
            mesh.append((box, (
                src_x[r, c], src_y[r, c],
                src_x[r + 1, c], src_y[r + 1, c],
                src_x[r + 1, c + 1], src_y[r + 1, c + 1],
                src_x[r, c + 1], src_y[r, c + 1],
            )))
    return panel.transform(size, Image.MESH, mesh, resample=Image.BILINEAR)


def _place(rgba: Image.Image, zoom: float, fx: float, fy: float, gain: float,
           size: tuple[int, int]) -> Image.Image:
    """One layer, zoomed and offset by its own share of the camera move.

    A single affine resample straight into the output size. The obvious
    implementation — scale the whole layer up, then crop — resamples millions of
    pixels that are then thrown away, and at 30fps that dominated the render.
    """
    width, height = size
    z = max(1.0, 1.0 + (zoom - 1.0) * gain)
    # Source window that maps onto the output, and the slack the zoom leaves for
    # the layer to travel through without exposing the canvas edge.
    win_w, win_h = width / z, height / z
    slack_x, slack_y = width - win_w, height - win_h
    left = min(max(slack_x / 2 + fx * gain * slack_x, 0.0), slack_x)
    top = min(max(slack_y / 2 + fy * gain * slack_y, 0.0), slack_y)
    # AFFINE maps output -> source: src = (x/z + left, y/z + top).
    return rgba.transform(
        size, Image.AFFINE, (1.0 / z, 0.0, left, 0.0, 1.0 / z, top),
        resample=Image.BILINEAR,
    )
