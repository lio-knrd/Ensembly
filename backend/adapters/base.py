"""Abstract adapter interfaces — one per pipeline stage."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GeneratedScene:
    narration_text: str
    image_prompt: str
    scene_type: str  # "still" | "video"
    characters: list[dict] = field(default_factory=list)
    continuity_context: list[dict] = field(default_factory=list)


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
