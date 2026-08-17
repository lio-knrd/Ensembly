from backend.adapters.image import _reference_strength
from backend.pipeline import (
    _apply_character_context,
    _apply_composition_contract,
    _apply_continuity_context,
    _image_prompt_and_style_for_scale,
    _is_distant_character_shot,
)


def test_reference_strength_is_conservative_and_clamped():
    assert _reference_strength(None, 0) == 0.2
    assert _reference_strength([0.12, 0.35], 1) == 0.35
    assert _reference_strength([-1.0], 0) == 0.0
    assert _reference_strength([2.0], 0) == 1.0


def test_character_context_does_not_repeat_scene_specific_sheet_description():
    prompt = _apply_character_context(
        "Zeus sits disguised in a plain travel cloak inside a smoky hall.",
        [{
            "name": "Zeus",
            "description": "white-and-gold himation, lightning, and thunderbolt",
        }],
        "character identity reference image",
    )

    assert "white-and-gold himation" not in prompt
    assert "current scene description is authoritative" in prompt
    assert "Do not copy" in prompt


def test_extreme_wide_shots_skip_krea_character_style_refs():
    assert _is_distant_character_shot("Extreme wide shot over an empty sea")
    assert _is_distant_character_shot("Wide shot with two tiny figures on a raft")
    assert not _is_distant_character_shot("Medium-wide shot of two people working")


def test_distant_composition_contract_forbids_portrait_overlays():
    prompt = _apply_composition_contract(
        "Extreme wide shot of a tiny raft.",
        "Extreme wide shot of a tiny raft.",
    )
    assert "one continuous cinematic frame" in prompt
    assert "every named person must remain small" in prompt
    assert "close-up overlay" in prompt


def test_dwarfed_cast_uses_environment_first_prompt_and_style():
    prompt, style = _image_prompt_and_style_for_scale(
        "Extreme wide shot: Deucalion and Pyrrha are dwarfed by the empty sea.",
        "expressive faces, cinematic action-panel composition, vertical webtoon cover quality",
        [{"name": "Deucalion"}, {"name": "Pyrrha"}],
    )
    assert "Deucalion" not in prompt and "Pyrrha" not in prompt
    assert "tiny indistinct human figure" in prompt
    assert "expressive faces" not in style
    assert "action-panel" not in style


def test_continuity_context_limits_reference_to_named_anchor():
    prompt = _apply_continuity_context(
        "A distant view of the raft at sea.",
        [{
            "scene_number": 3,
            "visual_anchor": "square wooden chest raft",
            "reason": "same vessel",
            "prompt": "A close portrait on a stormy hillside.",
        }],
        "continuity reference image",
        1,
    )

    assert "square wooden chest raft" in prompt
    assert "do not copy the earlier image's camera" in prompt
    assert "A close portrait on a stormy hillside" not in prompt
