"""Abstract adapter interfaces — one per pipeline stage."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GeneratedScene:
    narration_text: str
    image_prompt: str
    scene_type: str  # "still" | "video" | "animation"
    # Motion only — what moves and how the camera moves. Sent to the video model
    # instead of image_prompt, which describes a frozen frame. Empty for stills.
    motion_prompt: str = ""
    # Camera move for the shot. Drives the render-time move over a still, and
    # rides along as a hint on video scenes. See models.CameraMove.
    camera_move: str = "push_in"
    # Extra layers over the camera move, both off by default. See models.
    particles: str = "none"
    transition: str = "cut"
    characters: list[dict] = field(default_factory=list)
    continuity_context: list[dict] = field(default_factory=list)
    # Present only for scene_type == "animation": {"code", "title"} — AI-authored
    # Manim scene code (see backend/animation/catalog.py). None for still/video.
    animation: dict | None = None


@dataclass
class GeneratedScript:
    scenes: list[GeneratedScene]
    metadata: dict


@dataclass
class TTSResult:
    audio_path: Path
    timestamps_path: Path
    duration_seconds: float


class ScriptGenerator(ABC):
    """Layer-combined LLM call producing the fixed structured script object."""

    name: str = "abstract"

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict,
        topic: str,
        target_duration_seconds: int,
    ) -> GeneratedScript:
        ...


class TTSGenerator(ABC):
    """Per-scene text-to-speech producing audio + word-level timestamps."""

    name: str = "abstract"

    @abstractmethod
    def synthesize(self, text: str, audio_out: Path, timestamps_out: Path) -> TTSResult:
        ...


class ImageGenerator(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate(
        self,
        prompt: str,
        out_path: Path,
        reference_images: list[Path] | None = None,
    ) -> Path:
        ...


class VideoGenerator(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate(
        self,
        image_path: Path,
        prompt: str,
        out_path: Path,
        duration_seconds: float,
        elements: list[tuple[Path, list[Path]]] | None = None,
        end_image_path: Path | None = None,
    ) -> Path:
        """Generate a motion clip from a still.

        `elements` — per-character identity refs as (frontal_sheet, [variants]).
        `end_image_path` — optional final frame (e.g. the next scene's still).
        """
        ...


class AnimationGenerator(ABC):
    """Render a deterministic animation clip from AI-authored Manim code.

    ``spec`` is ``{"code", "title"}`` from ``backend/animation/catalog.py``;
    ``words`` is the scene's ElevenLabs word list (``[{word, start, end}, ...]``)
    injected so the code's ``self.cue("phrase")`` can bind events to the voice.
    The output MP4 is sized to ``duration_seconds`` at ``size`` (width, height) —
    matched to the scene's audio like a video clip. Raises on render failure with
    the engine's error text so callers can auto-repair.
    """

    name: str = "abstract"

    @abstractmethod
    def render(
        self,
        spec: dict,
        words: list[dict],
        out_path: Path,
        duration_seconds: float,
        size: tuple[int, int] = (1080, 1920),
    ) -> Path:
        ...
