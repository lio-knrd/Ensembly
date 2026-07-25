"""The three-layer prompt system (spec section 5).

Layer 1 (core system prompt) is FIXED and lives here in code — it is the tool's
contract with itself and guarantees every generation returns the same structured
script object regardless of which platform/content preset is active.

Layers 2 (platform preset) and 3 (content preset) are editable and stored in the
DB; they are combined with Layer 1 and the project's idea at generation time.
"""
from __future__ import annotations

import copy

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
    match them to their reference images. Make each prompt usable on its own.
  - continuity_context: an array of continuity links to earlier scene images. Use \
    this ONLY when this scene genuinely needs a previously established prop, \
    location, costume detail, symbol, vehicle, artifact, or environment to stay \
    visually consistent. Each item must include source_scene (the earlier 1-based \
    scene number), visual_anchor (the exact recurring visual element), and reason \
    (why that earlier image should be referenced). If no earlier image is needed, \
    return an empty array. Do not add links for general mood, theme, color, camera \
    angle, characters, or broad setting similarity.
  - scene_type: one of the allowed values in the response schema (at minimum \
    "still" and "video"). Default to "still". Mark a small number of pivotal \
    "hero" moments as "video" when motion would add real impact.
  - characters: a list of named characters that appear in this scene. Each entry \
    must include name, state, state_importance, and state_notes. Use consistent \
    names across scenes so the pipeline can lock character identity with reference \
    images. Leave state empty and state_importance as "default" for ordinary \
    appearances. Use a non-empty state ONLY for major identity/form differences \
    that need a different reference image, such as pre-curse vs post-curse, human \
    disguise vs divine/monster form, child vs adult, living vs undead, or a mortal \
    version vs transformed version. Do NOT use state for clothing, armor, pose, \
    mood, lighting, temporary wounds, hairstyles, props, or scene-specific styling.

Pace the total narration to fit the target duration the user provides \
(assume roughly 2.5 spoken words per second). Also produce social metadata for \
the finished video: a title, a description, and platform-appropriate hashtags. \
Create thumbnail copy with two distinct levels: cover_kicker is a short, \
intriguing context line of 2-5 words, while cover_title is the bold 1-3 word \
subject or name that should dominate the cover. Do not put "Part 1", "Part 2", \
or similar series numbering in either field; the pipeline adds that separately.

Return ONLY the structured object defined by the response schema. Do not add \
commentary before or after it.
"""


# --------------------------------------------------------------------------- #
# Animation guidance (added to the user turn only when the content preset
# opts in via enable_animations). Keeps non-animation prompts byte-identical.
# --------------------------------------------------------------------------- #
def _animation_guidance(latex_available: bool = False) -> str:
    return (
        "## Animations (this content supports deterministic diagrams)\n"
        "Some scenes explain a mathematical or scientific idea far better with a "
        "precise, programmatically-drawn diagram (Manim) than with a photo or AI "
        "video — a graph, a curve being drawn, the area under a curve, a number "
        "counting up, a geometric construction, vectors, a matrix. For such a scene "
        "set scene_type to \"animation\". You do NOT write any animation code here: "
        "the diagram is generated automatically later — AFTER the narration audio "
        "exists — from this scene's narration_text, so it can be synced to the real "
        "voice timings. Still write a short, self-contained image_prompt as a visual "
        "fallback.\n\n"
        "Choosing the scene_type — decide by what the frame literally SHOWS, not by "
        "how pivotal the moment feels. If the visual is a diagram, graph, chart, "
        "plot, number line, axes, vectors, a matrix or table, a geometric figure or "
        "construction, a counter, an equation or formula, labeled terms, OR a clean "
        "\"card\" made of text plus an equation plus simple shapes on the dark "
        "background (summary / recap / definition / title / worked-example / "
        "check-yourself cards), choose \"animation\" — Manim draws these crisply and "
        "exactly, animates each part in, and can restate an earlier equation or "
        "label 1:1 for visual continuity, which image generation cannot. Concretely: "
        "a straight line with \"f(x) = m x + b\" glowing above it and a checkmark, or "
        "a cost example resolving to m and b, is an \"animation\", NOT a \"still\". "
        "Reserve \"still\" and \"video\" for genuinely photographic or scenic "
        "real-world imagery — people, places, objects, atmosphere — that a diagram "
        "cannot convey. When a scene is mostly text and math on a plain background, "
        "it is almost always an animation. If a scene's continuity_context restates "
        "an equation/label from an earlier scene, strongly prefer \"animation\" so "
        "the restatement is pixel-identical.\n\n"
        "You do not need to pack a whole explanation into one animation scene: mark "
        "each diagrammatic beat as its own \"animation\" scene with its own "
        "narration. Consecutive animation scenes (with no still or video scene "
        "between them) are automatically combined into ONE continuous animation with "
        "one continuous narration and one voice take, so just write each beat "
        "naturally and let the pipeline join them."
    )


def build_script_prompt(
    platform_prompt: str,
    content_prompt: str,
    topic: str,
    target_duration_seconds: int,
    enable_animations: bool = False,
    latex_available: bool = False,
) -> str:
    """Assemble the full user-facing instruction: platform + content + project.

    The core system prompt (Layer 1) is passed separately as the system prompt;
    this function combines Layer 2 (platform), Layer 3 (content), and the
    project's one-line idea + target duration into the user turn. When the
    content preset enables animations, the animation template catalog is appended.
    """
    parts = [
        "## Platform / delivery format (how to package this)",
        platform_prompt.strip() or "(no platform guidance provided)",
        "",
        "## Content / subject matter and tone (what this is about)",
        content_prompt.strip() or "(no content guidance provided)",
        "",
    ]
    if enable_animations:
        parts += [_animation_guidance(latex_available), ""]
    parts += [
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
        "names and clear actions/positions in the image_prompt. In characters, "
        "leave state empty unless this scene needs a major identity/form state "
        "with its own reference sheet. For each scene, "
        "fill continuity_context with explicit earlier source_scene references "
        "ONLY when an earlier image is truly needed to preserve a specific "
        "object, place, artifact, costume detail, symbol, vehicle, or environment. "
        "Use an empty array for ordinary scene-to-scene flow or vague similarity.",
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
                    "continuity_context": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_scene": {"type": "integer"},
                                "visual_anchor": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["source_scene", "visual_anchor", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "scene_type": {"type": "string", "enum": ["still", "video"]},
                    "characters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "state": {"type": "string"},
                                "state_importance": {
                                    "type": "string",
                                    "enum": ["default", "major_identity_change", "ambiguous"],
                                },
                                "state_notes": {"type": "string"},
                            },
                            "required": ["name", "state", "state_importance", "state_notes"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["narration_text", "image_prompt", "continuity_context", "scene_type", "characters"],
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
                "cover_kicker": {"type": "string"},
                "cover_title": {"type": "string"},
            },
            "required": [
                "title", "description", "hashtags", "suggested_caption",
                "cover_kicker", "cover_title"
            ],
            "additionalProperties": False,
        },
    },
    "required": ["scenes", "metadata"],
    "additionalProperties": False,
}


def build_script_schema(enable_animations: bool = False) -> dict:
    """The structured-output schema, optionally extended for animation scenes.

    With animations off this is byte-for-byte ``SCRIPT_JSON_SCHEMA`` (so the
    default mythology path is unchanged). With animations on, the only change is
    that "animation" joins the scene_type enum. The LLM does NOT author any Manim
    code here — it merely picks the scene_type; the diagram is generated later,
    after audio exists, from the scene's narration (see backend/adapters/llm.py
    ``author_manim_code`` and pipeline ``_render_scene_animation``).
    """
    if not enable_animations:
        return SCRIPT_JSON_SCHEMA
    schema = copy.deepcopy(SCRIPT_JSON_SCHEMA)
    scene = schema["properties"]["scenes"]["items"]
    scene["properties"]["scene_type"]["enum"] = ["still", "video", "animation"]
    return schema
