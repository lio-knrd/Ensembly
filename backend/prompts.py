"""The three-layer prompt system (spec section 5).

Layer 1 (core system prompt) is FIXED and lives here in code — it is the tool's
contract with itself and guarantees every generation returns the same structured
script object regardless of which platform/content preset is active.

Layers 2 (platform preset) and 3 (content preset) are editable and stored in the
DB; they are combined with Layer 1 and the project's idea at generation time.
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# Layer 1 — Core system prompt (fixed, not editable, lives in code)
# --------------------------------------------------------------------------- #
CORE_SYSTEM_PROMPT = """\
You are the script engine of a semi-automated pipeline that produces short-form \
narrated videos (TikTok / YouTube Shorts / Reels style). Your output is consumed \
directly by downstream automated stages: text-to-speech with word-level \
timestamps, per-scene image generation, optional image-to-video generation, and \
a final rendered video with burned-in captions.

Because your output is machine-consumed, you MUST always return the same \
structured result: an ordered list of scenes. This structure is a fixed contract \
and never changes, regardless of the platform or topic presets that follow.

For each scene you must provide:
  - narration_text: the exact words to be spoken for this scene. Write for the \
    ear: natural, spoken-word phrasing, no stage directions, no emoji, no \
    markdown, no bracketed notes. This text is fed verbatim to text-to-speech.
  - image_prompt: a vivid, self-contained prompt for an image model describing \
    the visual for this scene. Describe subject, setting, mood, composition, and \
    camera/framing. Do NOT include named art styles, renderer styles, medium \
    styles, or aesthetic labels; the visual style is applied later from the \
    active content preset. If named characters appear, include their exact names \
    and role/action in the image_prompt so downstream image and video models can \
    match them to their reference images. Only when a later scene genuinely needs \
    a previously established prop, location, costume detail, symbol, vehicle, \
    artifact, or environment to stay visually consistent, describe that recurring \
    element again with the same concrete visual traits. Do not carry over unrelated \
    earlier scene details, and do not reference other scenes by number; make each \
    prompt usable on its own.
  - scene_type: either "still" or "video". Default to "still". Mark a small \
    number of pivotal "hero" moments as "video" when motion would add real impact.
  - characters: a list of named characters that appear in this scene (e.g. \
    ["Zeus", "Theseus"]), or an empty list. Use consistent names across scenes so \
    the pipeline can lock character identity with reference images.

Pace the total narration to fit the target duration the user provides \
(assume roughly 2.5 spoken words per second). Also produce social metadata for \
the finished video: a title, a description, and platform-appropriate hashtags.

Return ONLY the structured object defined by the response schema. Do not add \
commentary before or after it.
"""


def build_script_prompt(
    platform_prompt: str,
    content_prompt: str,
    topic: str,
    target_duration_seconds: int,
) -> str:
    """Assemble the full user-facing instruction: platform + content + project.

    The core system prompt (Layer 1) is passed separately as the system prompt;
    this function combines Layer 2 (platform), Layer 3 (content), and the
    project's one-line idea + target duration into the user turn.
    """
    parts = [
        "## Platform / delivery format (how to package this)",
        platform_prompt.strip() or "(no platform guidance provided)",
        "",
        "## Content / subject matter and tone (what this is about)",
        content_prompt.strip() or "(no content guidance provided)",
        "",
        "## This project",
        f"Topic / idea: {topic.strip()}",
        f"Target duration: about {target_duration_seconds} seconds.",
        "",
        "Write the full narrated script now, following the platform conventions "
        "and content tone above, and return the structured scene list plus social "
        "metadata. Keep image_prompt values style-neutral: describe only the "
        "scene content, mood, composition, lighting, and framing. Do not include "
        "art style words because image/video style is applied separately from "
        "the content preset. When a scene includes characters, put their exact "
        "names and clear actions/positions in the image_prompt. Only when later "
        "scenes genuinely continue earlier props, places, artifacts, costumes, "
        "or other context-bound objects that must stay visually consistent, "
        "repeat those elements with the same concrete visual traits. Do not "
        "carry over unrelated earlier scene details.",
    ]
    return "\n".join(parts)


# JSON schema describing the fixed structured output contract (Layer 1).
SCRIPT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "narration_text": {"type": "string"},
                    "image_prompt": {"type": "string"},
                    "scene_type": {"type": "string", "enum": ["still", "video"]},
                    "characters": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["narration_text", "image_prompt", "scene_type", "characters"],
                "additionalProperties": False,
            },
        },
        "metadata": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "suggested_caption": {"type": "string"},
                "hook_text": {"type": "string"},
            },
            "required": ["title", "description", "hashtags", "suggested_caption"],
            "additionalProperties": False,
        },
    },
    "required": ["scenes", "metadata"],
    "additionalProperties": False,
}
