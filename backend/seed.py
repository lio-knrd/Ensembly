"""Seed default presets and settings on first run.

Ships one default of each preset type: platform = "TikTok",
content = "Greek Mythology" (spec section 5).
"""
from __future__ import annotations

import json

from sqlmodel import Session, select

from .config import settings
from .database import engine
from .models import (
    Character,
    CharacterForm,
    ContentPreset,
    EditorialItem,
    PlatformPreset,
    Project,
    Setting,
    VisualMode,
)
from .services.music_library import (
    backfill_legacy_defaults,
    correct_whisper_credit,
    seed_local_library,
)
from .services.typography import dashless

# --------------------------------------------------------------------------- #
# Motion house styles
# --------------------------------------------------------------------------- #
# How things MOVE in a group's video scenes, handed to the image-to-video model
# alongside the scene's own motion_prompt. Deliberately free of look/lighting
# words: describing the style to an i2v model is what makes it paint effects
# over a still instead of animating it. Every one of these ends by ruling out
# invented light and particle effects, which is the failure mode these clips
# fall into when they have nothing concrete to move.
GENERIC_MOTION_STYLE = (
    "Move the shot, not the design. One clear action at a time, carried by a "
    "steady camera move rather than by animating everything at once. Secondary "
    "matter that hangs or floats — hair, cloth, smoke, dust, water, foliage — "
    "drifts gently and continuously so the frame never feels frozen. Keep the "
    "framing readable and the pace unhurried. Do not add lightning, glows, "
    "sparks, light rays, lens flares, or particle effects that are not already "
    "in the start frame, and do not redraw or restyle anything in it."
)

MOTION_STYLES_BY_PRESET: dict[str, str] = {
    "Greek Mythology": (
        "Limited 2D animation, the way real anime and webtoon adaptations move: "
        "the drawing largely holds and the CAMERA does the work — slow push-ins, "
        "gentle parallax drifts across the panel, a deliberate tilt to reveal "
        "scale. Cloth, hair, smoke, embers and falling debris drift continuously "
        "so the frame breathes. Character motion is one committed gesture — a "
        "head turn, a raised arm, a step forward, a weapon lifted — not full-body "
        "articulation and not running or fighting choreography. Weight is heavy "
        "and unhurried; gods move like they are certain. Do not add lightning, "
        "divine glows, energy auras, speed lines, sparks, light rays or lens "
        "flares that are not already drawn in the start frame."
    ),
    "Cat Cartoon": (
        "Broad, readable cartoon motion: one big clear action per shot with a "
        "little anticipation before it and a little settle after. Bouncy, springy "
        "timing rather than smooth realism, but keep it simple — a head turn, a "
        "hop, ears and tail flicking, a paw reaching. Faces stay expressive: "
        "blinks and small mouth movement even when the body holds. The camera "
        "moves plainly, a straight push-in or a level pan, never handheld. Do not "
        "add sparkles, glows, light rays, motion streaks or particle effects that "
        "are not already in the start frame, and keep the linework and colors "
        "exactly as drawn."
    ),
    "Math & Science (animated)": (
        "Calm, documentary motion. Real footage here exists to give the viewer "
        "somewhere to rest between diagrams, so it should never compete with "
        "them: a slow push-in or a level pan, one subject doing one ordinary "
        "thing, everything else still. No fast camera work, no whip pans, no "
        "dramatic reveals. Do not add glows, light rays, floating particles, "
        "data-viz overlays, numbers, or text of any kind — anything explanatory "
        "is drawn precisely in an animation scene instead."
    ),
    "Kaltgerechnet (DE)": (
        "Flat graphic motion, not character animation. Shapes translate, scale "
        "and reveal along straight paths at a constant, unfussy speed; the camera "
        "either holds or pushes in slowly. Think of elements sliding into place "
        "on a poster rather than a scene coming to life. Nothing wobbles, nothing "
        "bounces, nothing is hand-held. People, when present, make one small "
        "deliberate movement and hold. Do not add glows, gradients, shadows, "
        "light rays, sparks or particles, and do not add depth or shading that is "
        "not already in the start frame."
    ),
    "You Guessed Wrong (EN)": (
        "Flat graphic motion, not character animation. Silhouettes and shapes "
        "move along clean straight paths at a steady speed, or the camera pushes "
        "in slowly while they hold. One element moves at a time so the point of "
        "the frame stays obvious. No wobble, no bounce, no handheld camera, no "
        "crowd or background activity. Do not add glows, light rays, sparks, "
        "particles, gradients or shading, and do not add depth that is not "
        "already in the start frame."
    ),
}

DEFAULT_PLATFORM = PlatformPreset(
    name="TikTok",
    is_default=True,
    format_prompt=(
        "Target length ~60-90 seconds. Open with a strong hook in the first 1-2 "
        "seconds that stops the scroll — a bold question, a surprising claim, or a "
        "vivid image. Keep energy high and sentences short. End with either a "
        "satisfying payoff or a cliffhanger that invites a follow. "
        "Required social metadata: a punchy title (under 100 chars), a short "
        "description, and 5-10 relevant, discoverable hashtags suited to TikTok."
    ),
)

DEFAULT_CONTENT = ContentPreset(
    name="Greek Mythology",
    is_default=True,
    content_prompt=(
        "Retell classic Greek myths faithfully to the classical sources (Homer, "
        "Hesiod, Ovid, the tragedians). Use an engaging, slightly dramatic "
        "narrator voice that treats the gods and heroes as vivid, larger-than-life "
        "figures. Keep names, relationships, and the sequence of events accurate. "
        "Explain unfamiliar names briefly in-line. Favor wonder and stakes over "
        "dry exposition."
    ),
    # Applied to every generated image (character sheets + scenes) for a cohesive
    # look — kept out of the script prompt.
    image_style_prompt=(
        "Cinematic classical oil-painting style: warm dramatic lighting, painterly "
        "brushwork, epic mythological atmosphere, rich but muted color palette, "
        "consistent across all scenes. Vertical 9:16 composition."
    ),
    motion_style_prompt=MOTION_STYLES_BY_PRESET["Greek Mythology"],
    # Read as a manhwa: no generated video, many more panels, motion supplied by
    # the camera move over each panel. Kept as this group's default because it
    # is what the channel actually ships.
    visual_mode=VisualMode.PANELS,
    panel_seconds=4.0,
    voice_id=settings.elevenlabs_voice_id,
)

DEFAULT_MATH_CONTENT = ContentPreset(
    name="Math & Science (animated)",
    content_prompt=(
        "Explain a single mathematical or scientific idea clearly and vividly for "
        "a general audience. Build intuition step by step, define terms in plain "
        "language, and keep one tight through-line. Where a precise diagram — a "
        "graph, a curve, a count, a number line, or growing bars — explains better "
        "than a photo, use an animation scene and sync its cues to the narration."
    ),
    image_style_prompt=(
        "Clean, modern explanatory visuals on a dark background: crisp shapes, high "
        "contrast, minimal clutter, a restrained accent palette. Vertical 9:16."
    ),
    # The same look, expressed for drawn diagrams rather than generated images,
    # so animation scenes match the stills instead of using the catalog defaults.
    animation_style_prompt=(
        "Restrained and consistent: WHITE for structure (axes, gridlines, labels) "
        "and one accent color per scene for the quantity being explained. Numbers "
        "are large and sit in the upper third so the platform UI never covers "
        "them; labels are small and sit directly beneath what they name. No "
        "gradients, no glow, no decorative motion."
    ),
    motion_style_prompt=MOTION_STYLES_BY_PRESET["Math & Science (animated)"],
    enable_animations=True,
    voice_id=settings.elevenlabs_voice_id,
)

DEFAULT_SETTINGS: dict[str, object] = {
    "default_duration_seconds": 75,
    "default_platform_preset_name": "TikTok",
    "default_content_preset_name": "Greek Mythology",
    "active_image_model": settings.fal_krea_image_model,
}


def backfill_motion_styles(session: Session) -> None:
    """Give every existing group a motion house style, once.

    ``motion_style_prompt`` arrived after these groups were created, so without
    this they would all sit empty and their clips would go out with no motion
    guidance at all. Groups shipped with the app get the text written for their
    look; anything the creator made themselves gets the generic one, which is
    still far better than nothing. Guarded by a marker so a creator who later
    clears the field on purpose does not get it refilled on the next start.
    """
    if session.get(Setting, "backfilled_motion_styles") is not None:
        return
    for preset in session.exec(select(ContentPreset)).all():
        if (preset.motion_style_prompt or "").strip():
            continue
        preset.motion_style_prompt = MOTION_STYLES_BY_PRESET.get(
            preset.name, GENERIC_MOTION_STYLE
        )
        session.add(preset)
    session.add(Setting(key="backfilled_motion_styles", value=json.dumps(True)))


def switch_mythology_to_panels(session: Session) -> None:
    """Put the mythology group into panels mode, once.

    Generated video on detailed key art produced clips that slid the artwork
    around instead of animating it, so this group stops generating video and
    tells its stories in panels instead. One-time and marker-guarded: a creator
    who switches the group back to mixed keeps that choice.
    """
    if session.get(Setting, "mythology_panels_mode") is not None:
        return
    preset = session.exec(
        select(ContentPreset).where(ContentPreset.name == DEFAULT_CONTENT.name)
    ).first()
    if preset:
        preset.visual_mode = VisualMode.PANELS
        session.add(preset)
    session.add(Setting(key="mythology_panels_mode", value=json.dumps(True)))


def relax_mythology_pacing(session: Session) -> None:
    """Ship the more readable panel cadence to the existing mythology group once."""
    if session.get(Setting, "mythology_relaxed_pacing") is not None:
        return
    preset = session.exec(
        select(ContentPreset).where(ContentPreset.name == DEFAULT_CONTENT.name)
    ).first()
    if preset:
        preset.panel_seconds = 6.0
        session.add(preset)
    session.add(Setting(key="mythology_relaxed_pacing", value=json.dumps(True)))


def tighten_mythology_pacing(session: Session) -> None:
    """Take the mythology group back off its six-second panel hold, once.

    Six seconds was a correction to panels that flew past too fast, and it
    overshot: with the hold band on top, finished videos were holding images for
    ten seconds and running past four minutes. Four brings the cadence back to
    something a feed viewer stays with. One-time and marker-guarded, like the
    change it replaces, so a creator who sets their own hold keeps it.
    """
    if session.get(Setting, "mythology_tightened_pacing") is not None:
        return
    preset = session.exec(
        select(ContentPreset).where(ContentPreset.name == DEFAULT_CONTENT.name)
    ).first()
    if preset and preset.panel_seconds >= 6.0:
        preset.panel_seconds = DEFAULT_CONTENT.panel_seconds
        session.add(preset)
    session.add(Setting(key="mythology_tightened_pacing", value=json.dumps(True)))


# Copy a model wrote and a human reads. Prompts, narration and soundtrack
# credits are deliberately absent: the first two are instructions to a model,
# and the third is a licence obligation to reproduce a credit as given.
_DASHLESS_FIELDS: tuple[tuple[type, tuple[str, ...]], ...] = (
    (Project, ("title_card_kicker", "title_card_text", "title_card_part_label")),
    (EditorialItem, ("title", "summary", "coverage_summary", "part_group_title",
                     "ai_rationale")),
    (Character, ("description",)),
    (CharacterForm, ("description",)),
)


def dedash_existing_copy(session: Session) -> None:
    """Take the dashes out of copy written before the rule existed, once.

    New copy is cleaned where it is authored (services.typography), which does
    nothing for the plans, characters and covers already sitting in the app. A
    title a creator typed themselves is left exactly as they typed it.
    """
    if session.get(Setting, "dedashed_existing_copy") is not None:
        return
    for model, fields in _DASHLESS_FIELDS:
        for row in session.exec(select(model)).all():
            for field in fields:
                value = getattr(row, field, None)
                if isinstance(value, str) and dashless(value) != value:
                    setattr(row, field, dashless(value))
                    session.add(row)
    for project in session.exec(select(Project)).all():
        if not project.title_is_custom and dashless(project.title) != project.title:
            project.title = dashless(project.title)
            session.add(project)
    session.add(Setting(key="dedashed_existing_copy", value=json.dumps(True)))


def seed() -> None:
    with Session(engine) as session:
        seed_local_library(session)
        session.flush()
        correct_whisper_credit(session)
        backfill_legacy_defaults(session)
        if not session.exec(select(PlatformPreset)).first():
            session.add(DEFAULT_PLATFORM)
        if not session.exec(select(ContentPreset)).first():
            session.add(DEFAULT_CONTENT)
        # Ship an animations-enabled preset once, so the deterministic-animation
        # feature is discoverable. A marker keeps it from resurrecting if deleted.
        if session.get(Setting, "seeded_math_preset") is None:
            exists = session.exec(
                select(ContentPreset).where(ContentPreset.name == DEFAULT_MATH_CONTENT.name)
            ).first()
            if not exists:
                session.add(DEFAULT_MATH_CONTENT)
            session.add(Setting(key="seeded_math_preset", value=json.dumps(True)))
        session.flush()
        backfill_motion_styles(session)
        switch_mythology_to_panels(session)
        relax_mythology_pacing(session)
        tighten_mythology_pacing(session)
        dedash_existing_copy(session)
        for key, value in DEFAULT_SETTINGS.items():
            existing = session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value=json.dumps(value)))
        session.commit()
    # Runs after the commit because it reads the corrected track metadata and
    # writes files rather than rows. Idempotent, so no one-shot marker: a later
    # change to the credit wording is picked up by old projects on the next start.
    from .pipeline import refresh_music_attribution

    refresh_music_attribution()
