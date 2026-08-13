"""Depth-based 2.5D parallax for dolly moves over a still panel.

Parallax is a cue from camera translation, not from motion alone. Push-ins and
pull-outs approximate a camera travelling through the scene, so near and far
points should move at different rates. Pans and tilts approximate rotation
about the camera centre; punch-ins are editorial/optical zooms. Those moves
must keep the whole photograph coherent and therefore stay flat.

The panel is transformed through an edge-guided depth mesh. Near and far points
receive deliberately different camera gains, while a guided filter flattens
uncertain depth within surfaces and aligns changes with artwork edges. This is
strong enough to read as 2.5D without the hard seams produced by automatic
foreground/midground cutouts.

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

# Depth Anything V2 Small. The official preprocessing keeps the input aspect
# ratio, makes both dimensions at least 518, and rounds them to a multiple of
# the ViT patch size (14). Squashing a portrait panel into 518x518 changes what
# the model thinks a person and a room look like, even if the depth map is later
# stretched back to portrait.
_MODEL_NAME = "depth_anything_v2_small.onnx"
_INPUT_SIZE = 518
_PATCH_SIZE = 14
_DEPTH_CACHE_VERSION = 2
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

# Near/far camera gains are far enough apart to be perceptible in motion. The
# guided depth preparation below is what keeps that stronger field coherent.
_GAIN_FAR, _GAIN_NEAR = 0.70, 1.30
_MESH_COLS, _MESH_ROWS = 40, 72
_DEPTH_CAMERA_MOVES = frozenset({"push_in", "pull_out"})

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


def uses_depth(move: str) -> bool:
    """Whether a named move represents camera translation through the scene."""
    key = getattr(move, "value", move)
    return key in _DEPTH_CAMERA_MOVES


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


def _inference_size(size: tuple[int, int]) -> tuple[int, int]:
    """Depth Anything V2's aspect-preserving ``lower_bound`` resize.

    Returns ``(width, height)``. Both dimensions are at least 518 and divisible
    by 14, matching the preprocessing in the model's official implementation.
    """
    width, height = size
    scale = max(_INPUT_SIZE / max(1, width), _INPUT_SIZE / max(1, height))

    def lower_bound(value: float) -> int:
        rounded = int(round(value / _PATCH_SIZE) * _PATCH_SIZE)
        if rounded < _INPUT_SIZE:
            rounded = int(math.ceil(value / _PATCH_SIZE) * _PATCH_SIZE)
        return max(_PATCH_SIZE, rounded)

    return lower_bound(width * scale), lower_bound(height * scale)


def depth_map(image_path: Path, cache: bool = True) -> np.ndarray:
    """Normalised depth for a panel, 1.0 nearest and 0.0 furthest.

    Cached beside the image, because the same panel is re-rendered every time
    the project is re-rendered and the depth never changes.
    """
    cache_file = Path(image_path).with_suffix(f".depth-v{_DEPTH_CACHE_VERSION}.npy")
    if cache and cache_file.exists():
        try:
            return np.load(cache_file)
        except (OSError, ValueError):
            pass

    session = _get_session()
    image = Image.open(image_path).convert("RGB")
    image = image.resize(_inference_size(image.size), Image.BICUBIC)
    x = np.asarray(image, dtype=np.float32) / 255.0
    x = ((x - _IMAGENET_MEAN) / _IMAGENET_STD).transpose(2, 0, 1)[None]
    raw = session.run(None, {session.get_inputs()[0].name: x})[0][0]

    # Relative monocular depth has arbitrary scale and shift. Robust percentiles
    # stop one bad border pixel from flattening the useful depth range.
    lo, hi = np.percentile(raw, (2.0, 98.0))
    depth = np.clip((raw - lo) / (hi - lo), 0.0, 1.0) if hi > lo else np.zeros_like(raw)
    depth = depth.astype(np.float32)
    if cache:
        try:
            np.save(cache_file, depth)
        except OSError:
            pass
    return depth


def _render_depth(depth: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """A full-resolution, artwork-aligned relative-depth image for rendering."""
    image = _cover(Image.fromarray((depth * 255).astype(np.uint8)), size)
    image = image.filter(ImageFilter.MedianFilter(5)).filter(
        ImageFilter.GaussianBlur(max(1.0, size[0] / W * 1.8))
    )
    values = np.asarray(image, dtype=np.float32) / 255.0
    lo, hi = np.percentile(values, (2.0, 98.0))
    if hi - lo <= 1e-3:
        return np.zeros_like(values)
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0)


def _guided_mesh_depth(
    panel: Image.Image, depth: np.ndarray, size: tuple[int, int]
) -> np.ndarray:
    """Edge-guided, surface-smoothed depth sampled at mesh vertices.

    A guided filter flattens uncertain depth within similarly coloured surfaces
    but allows the field to change at artwork edges. This substantially reduces
    the rubber-sheet look of a raw depth warp without introducing the hard seams
    of automatic foreground/midground cutouts.
    """
    from scipy import ndimage

    p = _render_depth(depth, size)
    guidance = np.asarray(panel.convert("L"), dtype=np.float32) / 255.0
    radius = max(4, round(size[0] / W * 18))
    window = radius * 2 + 1
    mean_i = ndimage.uniform_filter(guidance, window, mode="reflect")
    mean_p = ndimage.uniform_filter(p, window, mode="reflect")
    corr_i = ndimage.uniform_filter(guidance * guidance, window, mode="reflect")
    corr_ip = ndimage.uniform_filter(guidance * p, window, mode="reflect")
    var_i = corr_i - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + 0.02)
    b = mean_p - a * mean_i
    q = ndimage.uniform_filter(a, window, mode="reflect") * guidance
    q += ndimage.uniform_filter(b, window, mode="reflect")
    q = np.clip(q, 0.0, 1.0)
    q = q * q * (3.0 - 2.0 * q)
    mesh = Image.fromarray(np.round(q * 255).astype(np.uint8), "L").resize(
        (_MESH_COLS + 1, _MESH_ROWS + 1), Image.BILINEAR
    )
    return np.asarray(mesh, dtype=np.float32) / 255.0


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
    scale = width / W
    field = []
    for _ in range(count):
        base_radius = rng.uniform(r_lo, r_hi)
        radius = base_radius * scale
        # Bigger motes read as nearer, so they fall faster and fade less.
        nearness = (base_radius - r_lo) / max(1e-6, r_hi - r_lo)
        field.append({
            "kind": kind,
            "x": rng.uniform(-0.05, 1.05) * width,
            "y": rng.uniform(-0.15, 1.15) * height,
            "r": radius,
            "color": color,
            "alpha": int(alpha * rng.uniform(0.45, 1.0)),
            "fall": fall * scale * (0.65 + 0.7 * nearness),
            "sway": sway * scale * rng.uniform(0.4, 1.3),
            "hz": sway_hz * rng.uniform(0.7, 1.4),
            "phase": rng.uniform(0, math.tau),
        })
    return field


def _draw_particles(field: list[dict], t: float, size: tuple[int, int]) -> Image.Image:
    """The particle layer at time ``t`` seconds.

    Screen space, drawn over the finished frame. Size and speed provide another
    depth cue: bigger, faster motes read as nearer than the warped artwork.
    """
    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    haze = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    haze_draw = ImageDraw.Draw(haze)
    for p in field:
        y = p["y"] + p["fall"] * t
        # Wrap with a margin so nothing pops in at the frame edge.
        span = height * 1.3
        y = ((y + height * 0.15) % span) - height * 0.15
        x = p["x"] + p["sway"] * math.sin(math.tau * p["hz"] * t + p["phase"])
        r = p["r"]
        color, alpha = p["color"], p["alpha"]
        # Soft translucent billboards read as atmospheric volume. The previous
        # opaque circles looked like interface dots pasted over the artwork.
        haze_draw.ellipse(
            (x - r * 1.8, y - r * 1.8, x + r * 1.8, y + r * 1.8),
            fill=(*color, max(1, alpha // 3)),
        )
        if p["kind"] == "embers":  # a short bright core along their motion
            tail = max(2.0, r * 2.4)
            draw.line(
                (x, y + tail, x, y - r * 0.4),
                fill=(*color, alpha), width=max(1, round(r * 0.7)),
            )
            draw.ellipse(
                (x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45),
                fill=(255, 226, 150, min(255, alpha + 20)),
            )
        elif p["kind"] == "petals":
            angle = p["phase"] + math.tau * 0.18 * t
            ux, uy = math.cos(angle), math.sin(angle)
            vx, vy = -uy * 0.38, ux * 0.38
            draw.polygon([
                (x + ux * r, y + uy * r),
                (x + vx * r, y + vy * r),
                (x - ux * r, y - uy * r),
                (x - vx * r, y - vy * r),
            ], fill=(*color, max(1, int(alpha * 0.7))))
        else:
            draw.ellipse(
                (x - r * 0.55, y - r * 0.55, x + r * 0.55, y + r * 0.55),
                fill=(*color, max(1, alpha // 2)),
            )
    haze = haze.filter(ImageFilter.GaussianBlur(max(0.7, size[0] / W * 2.2)))
    haze.alpha_composite(layer)
    return haze


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
    move, eligible depth parallax on top of it, and a particle overlay on top.
    ``transition`` blends the opening frames out of ``prev_frame`` — inside this
    panel's own duration, so the caption timeline never shifts.

    Depth is only eligible for push/pull dolly moves. A pan, tilt, static frame,
    or punch-in stays flat even when ``use_depth`` is true. Raises if depth was
    requested for an eligible move and is unavailable, so the caller can fall
    back rather than silently producing a flat clip that claims to be parallax.
    """
    depth_requested = use_depth and uses_depth(move)
    depth_on = depth_requested and available()
    if depth_requested and not depth_on:
        raise RuntimeError("depth model unavailable")

    width, height = size
    frames = max(1, int(round(duration * FPS)))
    panel = _cover(Image.open(image_path).convert("RGB"), size)

    mesh_depth = _guided_mesh_depth(panel, depth_map(image_path), size) if depth_on else None
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
    """Apply one continuous, edge-guided depth camera transform."""
    width, height = size
    gains = _GAIN_FAR + (_GAIN_NEAR - _GAIN_FAR) * mesh_depth
    z = np.maximum(1.0, 1.0 + (zoom - 1.0) * gains)
    win_w, win_h = width / z, height / z
    slack_x, slack_y = width - win_w, height - win_h
    left = np.clip(slack_x / 2 + fx * gains * slack_x, 0.0, slack_x)
    top = np.clip(slack_y / 2 + fy * gains * slack_y, 0.0, slack_y)

    xs = np.linspace(0, width, _MESH_COLS + 1)
    ys = np.linspace(0, height, _MESH_ROWS + 1)
    src_x = xs[None, :] / z + left
    src_y = ys[:, None] / z + top
    mesh = []
    for row in range(_MESH_ROWS):
        for col in range(_MESH_COLS):
            box = (int(xs[col]), int(ys[row]), int(xs[col + 1]), int(ys[row + 1]))
            mesh.append((box, (
                src_x[row, col], src_y[row, col],
                src_x[row + 1, col], src_y[row + 1, col],
                src_x[row + 1, col + 1], src_y[row + 1, col + 1],
                src_x[row, col + 1], src_y[row, col + 1],
            )))
    return panel.transform(size, Image.MESH, mesh, resample=Image.BICUBIC)


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
