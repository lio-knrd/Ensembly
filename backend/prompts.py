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
    match them to their reference images. Make each prompt usable on its own. \
    IMPORTANT, for scenes you mark "video": this image becomes the FIRST FRAME of \
    a generated clip, so describe the moment just BEFORE the action peaks, not the \
    peak itself — weight shifted and about to move, arm drawn back, cloak not yet \
    caught by the wind, the blow not yet landed. A frame already at the height of \
    the action has nowhere left to go and the video model will simply hold it. \
    Save the payoff for motion_prompt.
  - motion_prompt: what physically MOVES in this shot and how the camera moves. \
    Required for "video" scenes; use an empty string for "still" and "animation". \
    Write motion ONLY: which subject or body part moves, in which direction, how \
    fast, and what the camera does. Do NOT restate the setting, the lighting, the \
    mood, the colors, or the look — the start image already carries all of that, \
    and repeating it makes the model re-render the frame instead of moving it. One \
    or two plain sentences. Prefer one deliberate gesture plus one clear camera \
    move over several simultaneous actions. Never ask for lightning, glows, sparks, \
    light rays, energy, or particles that are not already visible in the start \
    image; that is the most common way these clips turn into a light show over a \
    frozen picture.
  - camera_move: the camera move for this shot, chosen from the values in the \
    response schema. For "still" scenes this is the only motion the viewer gets — \
    it is applied as a real camera move over the image at render time — so pick it \
    per scene and VARY it across the video rather than repeating one move. Match \
    the beat: push_in to build tension or land a revelation, pull_out to reveal \
    scale or consequence, pan_left/pan_right to travel across a landscape, a crowd, \
    or a line of figures, tilt_up for height and awe, tilt_down for a fall or a \
    descent, static only when stillness is the point, punch_in for a beat that is \
    meant to hit. For "video" scenes it should agree with the camera move you \
    described in motion_prompt.
  - particles: an optional drifting overlay on this panel, from the values in the \
    response schema. It is an extra layer on top of camera_move, never a \
    substitute for it — a panel can have a push-in and falling ash at once. \
    Default to "none" and mean it. Across a whole video no more than roughly one \
    panel in four should carry particles, and only where the place itself earns \
    them: ash in a burning or ruined place, embers over a fire or a forge, petals \
    in blossom or at a wedding, snow in winter or on a peak, dust in a tomb, a \
    ruin, a desert, or a shaft of light. Never add particles just to make a panel \
    livelier. Three panels of drifting petals in a row is worse than none, and it \
    is the clean panels that make the next dusty one land.
  - transition: how this panel arrives from the panel before it, from the values \
    in the response schema. Default to "cut" — a comic reads in cuts, and almost \
    every panel should be one. Use the others only where the story actually turns: \
    "fade" across a jump in time or place, "slide_up" or "slide_left" to carry \
    momentum through a fall, a chase, or a descent, and "flash" for a violent or \
    revelatory beat such as a blow landing, a death, or a god appearing. A handful \
    in a whole video, not one every few panels. The first scene must be "cut": \
    there is nothing for it to arrive from.
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
Write one description that works as-is on every platform — it is used verbatim \
as the YouTube description and as the TikTok caption, so do not write it for one \
platform in particular and do not put the hashtags inside it (they are appended \
automatically). Create thumbnail copy with two distinct levels: cover_kicker is a short, \
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
        "naturally and let the pipeline join them.\n\n"
        "Numbers must be worked out, never just asserted. If the video uses a "
        "concrete numeric example, the narration must walk through the actual "
        "calculation in steps the viewer can follow: state the inputs, say which "
        "operation is applied to them, and speak the intermediate results on the way "
        "to the answer. Do not jump from the setup straight to a final figure, and "
        "do not present a number the script never derived. Every figure you speak "
        "must be arithmetically correct and reachable from the numbers spoken before "
        "it, so a viewer with a calculator gets the same result. Because the diagram "
        "is written from the narration, a calculation the narration skips cannot be "
        "drawn either."
    )


def _panel_guidance(panel_seconds: float) -> str:
    """Panel-mode authoring rules (added only when the group is in PANELS mode).

    Nothing here is about motion: the sense of movement comes from the camera
    move chosen per panel and applied at render time. What this block buys is
    density and specificity — many short beats, each with a real place in it,
    instead of a handful of long scenes with vague backgrounds.
    """
    return (
        "## Panels (this group is read like a manhwa, not watched like a film)\n"
        "There is NO generated video in this group. Every scene is one still "
        "panel, and the only motion the viewer sees is the camera move applied "
        "over that panel at render time. Write accordingly.\n\n"
        f"Break the story into many short beats — aim for roughly "
        f"{panel_seconds:.0f} seconds of narration per panel, so a longer video "
        "becomes a lot of panels rather than a few slow ones. A beat is one "
        "moment: a decision, a reaction, an arrival, a line landing, a detail "
        "the viewer should notice. When a sentence contains two moments, split "
        "it into two panels.\n\n"
        "Every image_prompt must put the panel somewhere specific. Name the "
        "place, the time of day, the weather, and what the light is doing, and "
        "describe the foreground, the middle ground and the background as "
        "separate layers — what is close to us, what the subject stands in, "
        "what sits far behind. Include the concrete props and textures that "
        "belong to that place. A panel described only as a character against a "
        "vague mood is a wasted panel.\n\n"
        "Vary the shot scale deliberately across consecutive panels, the way a "
        "real manhwa page does: an establishing wide to place the reader, a "
        "medium for dialogue and action, a close-up for emotion, a tight insert "
        "on a hand or an object or an eye, a reaction shot. Never run three "
        "panels at the same scale in a row. The same applies to camera_move — "
        "choose the one that fits each beat and keep it changing, and use "
        "punch_in for the moments that are meant to hit."
    )


def _revision_notes_block(revision_notes: str) -> str:
    """The creator's correction notes for this project, as its own section.

    Placed last so it is the final thing the model reads, and worded as an
    override: these notes exist precisely because an earlier attempt got
    something wrong, so they outrank the preset's general guidance.
    """
    return (
        "## Correction notes from the creator (highest priority)\n"
        "An earlier script for this project was rejected. Treat the notes below as "
        "binding instructions for this rewrite: fix every problem they name, and "
        "where they conflict with the general platform or content guidance above, "
        "follow the notes. Do not mention the notes, the rewrite, or any earlier "
        "version in the narration.\n"
        f"{revision_notes.strip()}"
    )


def build_script_prompt(
    platform_prompt: str,
    content_prompt: str,
    topic: str,
    target_duration_seconds: int,
    enable_animations: bool = False,
    latex_available: bool = False,
    revision_notes: str = "",
    panels_mode: bool = False,
    panel_seconds: float = 4.0,
) -> str:
    """Assemble the full user-facing instruction: platform + content + project.

    The core system prompt (Layer 1) is passed separately as the system prompt;
    this function combines Layer 2 (platform), Layer 3 (content), and the
    project's one-line idea + target duration into the user turn. When the
    content preset enables animations, the animation template catalog is appended.
    ``revision_notes`` is the project's creator feedback (empty for a first
    generation), appended last as a binding correction section.
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
    if panels_mode:
        parts += [_panel_guidance(panel_seconds), ""]
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
        "names and clear actions/positions in the image_prompt. For every scene "
        "pick a camera_move that fits that beat, and vary it across the video — a "
        "whole video on one repeated move looks mechanical. "
        + (
            "Leave motion_prompt empty on every scene: this group generates no "
            "video, so there is nothing for it to drive. "
            if panels_mode
            else "For \"video\" scenes write the image_prompt as the moment BEFORE "
            "the action and put the action itself in motion_prompt, keeping "
            "motion_prompt to movement and camera only with no scenery, lighting, "
            "or style words in it. "
        )
        + "In characters, "
        "leave state empty unless this scene needs a major identity/form state "
        "with its own reference sheet. For each scene, "
        "fill continuity_context with explicit earlier source_scene references "
        "ONLY when an earlier image is truly needed to preserve a specific "
        "object, place, artifact, costume detail, symbol, vehicle, or environment. "
        "Use an empty array for ordinary scene-to-scene flow or vague similarity.",
    ]
    if revision_notes.strip():
        parts += ["", _revision_notes_block(revision_notes)]
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
                    "motion_prompt": {"type": "string"},
                    "camera_move": {
                        "type": "string",
                        "enum": [
                            "static", "push_in", "pull_out",
                            "pan_left", "pan_right", "tilt_up", "tilt_down",
                            "punch_in",
                        ],
                    },
                    "particles": {
                        "type": "string",
                        "enum": ["none", "dust", "petals", "embers", "snow", "ash"],
                    },
                    "transition": {
                        "type": "string",
                        "enum": ["cut", "fade", "slide_up", "slide_left", "flash"],
                    },
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
                "required": [
                    "narration_text", "image_prompt", "motion_prompt", "camera_move",
                    "particles", "transition",
                    "continuity_context", "scene_type", "characters",
                ],
                "additionalProperties": False,
            },
        },
        "metadata": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "hook_text": {"type": "string"},
                "cover_kicker": {"type": "string"},
                "cover_title": {"type": "string"},
            },
            "required": [
                "title", "description", "hashtags",
                "cover_kicker", "cover_title"
            ],
            "additionalProperties": False,
        },
    },
    "required": ["scenes", "metadata"],
    "additionalProperties": False,
}


def build_script_schema(
    enable_animations: bool = False, panels_mode: bool = False
) -> dict:
    """The structured-output schema, adjusted for the group's capabilities.

    With animations off and panels off this is byte-for-byte
    ``SCRIPT_JSON_SCHEMA``. Animations add "animation" to the scene_type enum;
    panels mode REMOVES "video" from it, which is what actually guarantees a
    panels group can never run up a generation bill — the model has no way to
    express a video scene rather than merely being asked not to.
    """
    if not enable_animations and not panels_mode:
        return SCRIPT_JSON_SCHEMA
    schema = copy.deepcopy(SCRIPT_JSON_SCHEMA)
    scene = schema["properties"]["scenes"]["items"]
    types = ["still"]
    if not panels_mode:
        types.append("video")
    if enable_animations:
        types.append("animation")
    scene["properties"]["scene_type"]["enum"] = types
    return schema
