"""The three-layer prompt system (spec section 5).

Layer 1 (core system prompt) is FIXED and lives here in code — it is the tool's
contract with itself and guarantees every generation returns the same structured
script object regardless of which platform/content preset is active.

Layers 2 (platform preset) and 3 (content preset) are editable and stored in the
DB; they are combined with Layer 1 and the project's idea at generation time.
"""
from __future__ import annotations

import copy

from .services.pacing import MAX_RUNTIME_FACTOR, panel_hold_band

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
    momentum through a fall, a chase, or a descent, "flash" for a violent or \
    revelatory beat such as a blow landing, a death, or a god appearing, \
    "whip_pan" to snap between two places or two people in the same instant, and \
    "impact_cut" for the panel where a blow, a fall or a door actually lands — it \
    cuts hard and shakes the frame rather than blending. A handful in a whole \
    video, not one every few panels. The first scene must be "cut": there is \
    nothing for it to arrive from.
  - motion_fx: an impact effect drawn over this panel, from the values in the \
    response schema. Unlike particles, which drift for the whole panel, these hit \
    in the first half second and are gone. "speed_lines" draws the comic's radial \
    streaks and belongs on the panel where a strike or a charge connects; \
    "impact_shake" jolts the frame for a blow landing, an earthquake, a giant's \
    step, a door breaking; "motion_blur" smears the panel along its own camera \
    move and suits a fall, a chase, a body thrown. Default to "none". These are \
    punctuation, not the motion itself — the motion belongs in the pictures, and \
    an effect only marks the instant one of them lands. So a talking or \
    establishing panel takes none, and even inside a fight only the panel where \
    the blow actually connects carries one. Never put the same effect on \
    consecutive panels, and never use one to make a static picture feel busier.
  - grade: a colour treatment laid over this panel at render time, from the \
    values in the response schema. This is where the emotion of a beat lives: \
    "warm" for safety, home, a fire, a reunion or a small kindness, "cold" for \
    dread, distance, a cruel decision or a place that does not want you there, \
    "blood" for violence, rage and the moment something dies, "moonlight" for \
    night, the supernatural and the uncanny, "memory" for a flashback or \
    anything being remembered rather than happening. Default to "none", which \
    should still be the most common answer, and let the artwork's own palette \
    carry most panels. A grade belongs to the whole beat, so every panel sharing \
    a beat_id takes the same one, and it changes when the feeling changes — a \
    colour that shifts inside a continuous scene breaks it apart. Do not grade a \
    panel merely because it is dark or bright; grade it because the story has \
    turned.
  - beat_id: a short lowercase slug naming the continuous scene this panel \
    belongs to, such as "lion-wrestle" or "flight-over-the-sea". Consecutive \
    panels that share a beat_id are read as one moment seen from several angles, \
    the way a comic gives a single exchange a row of panels — so give a beat the \
    two to six panels it actually takes and change only the angle, the distance \
    and the action between them. The place, the time of day, the weather, the \
    light direction and the palette must stay the same for every panel in a \
    beat; when any of those genuinely changes, the beat has ended and a new \
    beat_id starts. A fight is the clearest case: the swing, the block, the \
    counter and the landing blow are four panels of one beat, not four scenes. \
    Use an empty string for a panel that stands alone, which is the right answer \
    for narration that moves through time or summarises, and expect a video to \
    be a mix of both.
    Inside a beat that carries action, every panel's image_prompt is a moment \
    caught partway through a movement, never a pose: the swing already \
    travelling, the body already leaving the ground, the spell already out of \
    the hand, the block taken with the weight already behind it. A row of still \
    portraits does not become a fight by being next to each other. The sense of \
    motion in this format comes from the pictures themselves and from what \
    changes between them, so make each panel a different instant of the same \
    movement, and let the position of the bodies, the weapons, the hair and the \
    cloth carry that difference.
  - continuity_context: an array of continuity links to earlier scene images, \
    used to hold a specific thing steady across panels. Each item must include \
    source_scene (the earlier 1-based scene number), visual_anchor (the exact \
    recurring visual element), and reason. Link to the previous panel of the \
    same beat whenever this panel continues a place or a staging the reader has \
    just been looking at, and to an earlier panel when a prop, location, costume \
    detail, symbol, vehicle or artifact returns after a gap. Do not link for \
    mood, theme or broad similarity alone, and do not link for characters, whose \
    identity is held by their own reference images. Return an empty array when \
    nothing earlier is needed.
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

THE OPENING. A viewer in a feed decides whether to stay inside the first two \
seconds, before the story has explained anything, so scene 1 is not the \
beginning of the story — it is the argument for watching it. Three things carry \
it, and all three must work on their own.

First, hook_text: one short sentence, at most about ten words, burned across \
the top of the frame for the opening two seconds. Write it as a concrete claim \
with a consequence in it, not a question and not a label. "Zeus drowned every \
human on earth" works; "The story of the great flood", "Greek mythology \
explained" and "Who really rules Olympus?" do not, because a title names a \
subject and a rhetorical question can be answered with a shrug and a scroll. \
Name a specific person or thing, and say the surprising part out loud rather \
than promising it is coming. It may spoil a late beat — a viewer who stays for \
the spoiler is the point.

Second, the first narration_text. Open on the sharpest concrete moment in the \
whole story, even when that moment belongs chronologically later; the \
background can follow once someone is listening. Do not open with scene \
setting, a date, a genealogy, a "long ago" or an address to the audience. The \
first spoken sentence should be able to stand alone as the reason to keep \
watching, and it must not merely restate hook_text word for word — the eye and \
the ear should get two different pieces of the same moment.

Third, the first image_prompt, which is the single most important image in the \
video. A viewer must be able to tell what they are looking at in a fraction of a \
second on a phone, and the picture must carry either force or consequence. Build \
it as one of the kinds below — whichever the sharpest moment of this story \
genuinely is, and a different one from video to video.

MOTION: a moment caught partway through. A blow landing, a body falling, a thing \
shattering, a creature lunging into frame, someone running from what is already \
at their heels. Weight in the air, cloth and hair thrown by the movement, the \
action underway rather than about to begin.

DOMINION: a figure holding the high ground over what they have done, or are \
about to do. Put them high in the frame or at its near edge and keep them big \
enough to read as a person — their build, their dress, the set of their \
shoulders — and put the consequence below and beyond them, distant and small. \
The charge comes from the drop between the two, so it is the world that goes \
small and never the figure. Light them from behind and let their face be turned \
away; what is withheld reads as power.

ARRIVAL: something enormous entering the frame that the people in it have not \
fully seen yet. A head above the treeline, a hand coming down through cloud, a \
shadow falling across a crowd still looking the wrong way.

AFTERMATH: the consequence by itself, with whoever caused it small or absent. A \
petrified crowd, a drowned plain, a burnt hall. The viewer stays to find out \
what did this.

ARTIFACT: one object in extreme close-up at the moment it turns. Wax running off \
a feather, a hand going to stone, an eye with fire in it. A frame this tight \
stops a viewer but cannot hold one, so an opening like this gets a short first \
narration line and reaches the person or the place in the very next panel.

Commit to whichever kind you choose. A frame that is half action and half \
portrait is neither, and reads as somebody posing. Keep one subject and one idea \
in it: a second figure, a scatter of props and a landmark all competing in the \
same opening is clutter, and clutter is the thing a viewer scrolls past.

The frame needs one unmistakable focal point that separates from everything else \
in brightness and not only in colour: a lit figure against dark, or a dark one \
against fire, sky or dust. It has to stay readable as the thing it is — a person \
must still show as a person, with a build and clothes you could describe, never \
a speck somewhere in a landscape. Hold the light the rest of the video works in: \
deep shadow with one hard source. Flat overhead noon daylight washes the \
contrast out and drags the picture away from the series look. What always fails \
is an abstraction with nothing in it to look at, whether smoke, cloud, mist, \
void or swirl, and an empty background, and a crowd with no focal point. Write \
this image in your own words for this particular story, and do not carry \
phrasing over from these instructions.

THE ENDING. The last scene closes the beat it is on and opens the next one. \
End on the consequence that has not happened yet, the person who has not \
arrived, or the price not yet paid, in one line that names it specifically. Do \
not end with a summary of what was just told, a moral, or a request to follow, \
like, or comment.

Treat the target duration as a rough planning reference, never a hard cap. \
Narrative clarity, comprehension, and a satisfying dramatic rhythm outrank \
runtime; the project-specific prompt tells you how far the script may expand. \
Also produce social metadata for \
the finished video: a title, a description, and platform-appropriate hashtags. \
Write one description that works as-is on every platform — it is used verbatim \
as the YouTube description and as the TikTok caption, so do not write it for one \
platform in particular and do not put the hashtags inside it (they are appended \
automatically). Feeds truncate it after roughly the first line, so make that \
first sentence do the work of a hook on its own and put the recap, if any, \
after it. Never write a URL into the description. Create thumbnail copy with two distinct levels: cover_kicker is a short, \
intriguing context line of 2-5 words, while cover_title is the bold 1-3 word \
subject or name that should dominate the cover. cover_kicker is also set in \
small capitals above hook_text on the opening card, so write it as the name of \
the episode's arc or moment rather than as a sentence about it: "The Great \
Flood", "The First Woman", "The War For Olympus". No verb, no punctuation, and \
never the same words as cover_title or hook_text. Do not put "Part 1", "Part 2", \
or similar series numbering in either field; the pipeline adds that separately.

Every image_prompt and motion_prompt you write is sent to a moderated image or \
video model, and a single rejected prompt fails the entire render, so keep them \
safe for a general audience. Dress figures in the clothing of their own world — \
drapery, chiton, robes, armor, furs, swaddling for an infant — fully covering, \
in opaque fabric, and never describe a figure as nude, partly nude, undressed, \
or sexualised. Write about the garments and how they hang, not about the parts \
of the body they cover: "a heavy wool himation falling to her sandalled feet", \
never a list of anatomy the cloth passes over. Never write that someone wears \
only one thing, \
and never call fabric sheer, translucent, gauzy, see-through, clinging, or \
slipping: that phrasing alone gets the image refused, however it was meant. \
A figure may be young, old, an infant, beautiful, or an object of desire in the \
story — none of that is a problem, and you should not age anyone up or dress \
them out of their own century to be safe. Where the tradition shows a figure \
wearing very little, such as a goddess born from sea foam, keep what makes them \
recognisable and let drapery, long hair, water, foam, mist, or a foreground \
object carry the covering. Keep violence implied rather than shown: aftermath, \
shadow, silhouette, posture, and the reaction on a face, instead of gore, open \
wounds, dismemberment, or blood. This constrains only the visuals — the \
narration may tell the story fully.

Return ONLY the structured object defined by the response schema. Do not add \
commentary before or after it.
"""


# --------------------------------------------------------------------------- #
# Content limits for the image/video models
# --------------------------------------------------------------------------- #
# Every hosted image model moderates its own input, and a rejection is a hard
# stop for the pipeline step — there is no partial result and no retry of the
# same prompt that will pass. Mythology and folklore are where this bites:
# Aphrodite rising from the foam, a flayed titan, a battlefield. Stating the
# limits costs one paragraph per request and turns most of those into a usable
# picture instead of a content_policy failure. Applied at the adapter edge (see
# adapters/image.py, adapters/video.py) so no prompt can route around it.
#
# MEASURED, and the reason every line below says what TO draw: these clauses are
# read by the provider's own keyword filter before any image exists, and that
# filter does not parse negation. Submitted against Krea, a clause spelling out
# what to avoid was refused with content_policy ON ITS OWN — the prohibition
# vocabulary is what it matched, so the guardrail became the trigger. The
# positive phrasing here passes both alone and attached to a real prompt. Do not
# reintroduce a "no <thing>" list here; put that guidance in the LLM-facing
# prompts below, where the reader is a language model that understands "no".
#
# It is also deliberately narrow. An earlier draft demanded a "grown adult" in
# "complete, opaque clothing", which is wrong twice over: myth is full of
# infants and children (Hermes has a newborn form in the library), and burying
# a figure the tradition depicts lightly dressed in full robes loses the figure.
# The rule is coverage, not costume — the clothing named is the setting's own,
# and where the source shows very little, the scene itself does the covering.
#
# It also names no body parts, which is the third thing measured here. A clause
# reading "chest, hips and thighs stay behind opaque fabric" passed on its own
# and against most prompts, but was refused whenever the description carried an
# age cue — "a princess in her late teens" plus a second anatomy list reads as
# something neither half means alone. Both halves are innocent; the pairing is
# not. So the clause asks for full covering without inventorying what is
# covered, which tested clean against youthful and adult descriptions alike.
IMAGE_CONTENT_LIMITS = (
    "Content limits (mandatory): keep this image suitable for a general "
    "audience. Dress each figure in the clothing of their own world — "
    "classical drapery, chiton and himation, robes, armor, furs, swaddling, "
    "whatever the setting calls for — fully covering, in opaque fabric. Where "
    "the source tradition shows a figure lightly dressed, keep them "
    "recognisable as themselves and let flowing drapery, long hair, water, sea "
    "foam, mist, cloud or foliage complete the covering. Keep any conflict "
    "restrained, with figures whole and unharmed."
)

# The i2v model already has the picture, so the risk is narrower: a subject who
# is fine in the start frame losing clothing over the clip. Kept short because
# clip prompts are length-capped, and positive for the same measured reason.
VIDEO_CONTENT_LIMITS = (
    "Content limits (mandatory): everyone keeps the full, opaque clothing they "
    "already wear in the start image, and the clip stays modest and suitable "
    "for a general audience, with figures whole and unharmed."
)


def with_image_content_limits(prompt: str) -> str:
    """Append the image content limits, once."""
    return _append_once(prompt, IMAGE_CONTENT_LIMITS)


def with_video_content_limits(prompt: str) -> str:
    """Append the clip content limits, once."""
    return _append_once(prompt, VIDEO_CONTENT_LIMITS)


def _append_once(prompt: str, clause: str) -> str:
    prompt = (prompt or "").strip()
    if clause in prompt:
        return prompt
    return f"{prompt}\n\n{clause}" if prompt else clause


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
    floor, ceiling = panel_hold_band(panel_seconds)
    return (
        "## Panels (this group is read like a manhwa, not watched like a film)\n"
        "There is NO generated video in this group. Every scene is one still "
        "panel, and the only motion the viewer sees is the camera move applied "
        "over that panel at render time. Write accordingly.\n\n"
        f"Aim for a {floor:.1f}-to-{ceiling:.1f}-second "
        "hold for most panels, not a quota tied to the requested runtime. That is "
        "long enough to read an image and short enough that the page keeps turning; "
        "a panel held for ten seconds is what makes this format feel slow. Shorter "
        "panels are reserved for a rare impact beat; longer ones need a deliberate "
        "reason to linger. A panel may "
        "carry two naturally connected sentences when they belong to "
        "the same visual moment. Split when the place, action, point of "
        "view, or emotional beat actually changes. Never turn every clause into "
        "a new panel. The viewer needs time to read the image as well as hear the "
        "words.\n\n"
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


def _duration_guidance(target_duration_seconds: int) -> str:
    """Make runtime elastic without inviting padding or an unfocused script.

    The ceiling is the same multiple the automatic review measures against
    (services.pacing.MAX_RUNTIME_FACTOR), so a draft is not first invited to a
    length that is then sent back for a rewrite.
    """
    upper = max(target_duration_seconds, round(target_duration_seconds * MAX_RUNTIME_FACTOR))
    return (
        "## Runtime and listening pace (higher priority than platform length rules)\n"
        f"The requested {target_duration_seconds}-second duration is a rough "
        "short-form reference, NOT a deadline and NOT a word budget. The finished "
        f"narration may run longer when the story genuinely needs it, up to about "
        f"{upper} seconds ({upper // 60}:{upper % 60:02d}) — but that is a ceiling, "
        "not a destination. Most scripts should land nearer the reference. Never "
        "add filler merely to make it longer.\n\n"
        "This is watched in a feed, so it has to keep moving. Say things once. "
        "Cut recap, restatement, scene-setting the payoff does not need, and "
        "stacked adjectives. Prefer concrete verbs and sentences that carry the "
        "story forward.\n\n"
        "Within that, optimize for comprehension at a calm, dramatic spoken pace. "
        "Do not compress a chain of causes, transformations, unfamiliar names, or "
        "locations into a rapid chronicle just to hit the reference. Introduce "
        "one unfamiliar idea, let its consequence or human reaction land, then "
        "move on. Energy should come from stakes and specificity, not from racing "
        "through facts."
    )


def _pacing_feedback_block(feedback: str) -> str:
    return (
        "## Automatic pacing review of the previous draft\n"
        "The previous draft was rejected before audio or images were generated. "
        "Rewrite the entire script and fix this measured pacing problem without "
        "dropping essential story beats:\n"
        f"{feedback.strip()}"
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
    pacing_feedback: str = "",
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
    parts += [_duration_guidance(target_duration_seconds), ""]
    if enable_animations:
        parts += [_animation_guidance(latex_available), ""]
    if panels_mode:
        parts += [_panel_guidance(panel_seconds), ""]
    parts += [
        "## This project",
        f"Topic / idea: {topic.strip()}",
        f"Rough duration reference: about {target_duration_seconds} seconds (elastic as described above).",
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
    if pacing_feedback.strip():
        parts += ["", _pacing_feedback_block(pacing_feedback)]
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
                        "enum": ["cut", "fade", "slide_up", "slide_left", "flash",
                                 "whip_pan", "impact_cut"],
                    },
                    "beat_id": {"type": "string"},
                    "motion_fx": {
                        "type": "string",
                        "enum": ["none", "speed_lines", "impact_shake", "motion_blur"],
                    },
                    "grade": {
                        "type": "string",
                        "enum": ["none", "warm", "cold", "blood", "moonlight", "memory"],
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
                    "particles", "transition", "beat_id", "motion_fx", "grade",
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
                "title", "description", "hashtags", "hook_text",
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
