"""Per-stage generator adapters.

Each stage's model call sits behind a small interface (see base.py) with one
concrete implementation active at a time. Adding a new model later is a new
concrete class plus a config entry — no pipeline rewrite (spec section 4a).
"""
from .registry import (
    get_animation_generator,
    get_image_generator,
    get_script_generator,
    get_tts_generator,
    get_video_generator,
)

__all__ = [
    "get_script_generator",
    "get_tts_generator",
    "get_image_generator",
    "get_video_generator",
    "get_animation_generator",
]
