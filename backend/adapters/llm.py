"""Script-generation adapters (Anthropic default, OpenAI alternate, offline mock).

All three return the same fixed structured script object (Layer 1 contract).
"""
from __future__ import annotations

import json
import re
import textwrap

import httpx

from ..config import settings
from .base import GeneratedScene, GeneratedScript, ScriptGenerator


def _extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM text response.

    Tolerates code fences and any surrounding prose, so it works regardless of
    whether the model was constrained by a structured-output parameter.
    """
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


def _coerce_script(data: dict) -> GeneratedScript:
    scenes = []
    for raw in data.get("scenes", []):
        st = str(raw.get("scene_type", "still")).lower()
        if st not in ("still", "video"):
            st = "still"
        scenes.append(
            GeneratedScene(
                narration_text=str(raw.get("narration_text", "")).strip(),
                image_prompt=str(raw.get("image_prompt", "")).strip(),
                scene_type=st,
                characters=[str(c).strip() for c in raw.get("characters", []) if str(c).strip()],
            )
        )
    return GeneratedScript(scenes=scenes, metadata=data.get("metadata", {}))


class AnthropicScriptGenerator(ScriptGenerator):
    name = "anthropic"

    def generate(self, system_prompt, user_prompt, schema, topic, target_duration_seconds):
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_script_model,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return _coerce_script(_extract_json(text))


class OpenAIScriptGenerator(ScriptGenerator):
    name = "openai"

    def generate(self, system_prompt, user_prompt, schema, topic, target_duration_seconds):
        payload = {
            "model": settings.openai_script_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "script", "schema": schema, "strict": True},
            },
        }
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return _coerce_script(_extract_json(content))


class OfflineScriptGenerator(ScriptGenerator):
    """Deterministic placeholder script so the pipeline runs without an LLM key."""

    name = "offline"

    def generate(self, system_prompt, user_prompt, schema, topic, target_duration_seconds):
        topic = topic.strip() or "an untold story"
        # ~2.5 words/sec; aim for ~35 words/scene.
        total_words = max(60, int(target_duration_seconds * 2.5))
        n_scenes = max(3, min(8, round(total_words / 35)))
        cast = _guess_character_names(topic)

        beats = [
            f"Long before our time, the tale of {topic} began.",
            f"Few dared to speak of what {topic} truly meant.",
            f"The turning point came when everything about {topic} changed.",
            f"Against all odds, the heart of {topic} was revealed.",
            f"And so the legend of {topic} echoes down to us today.",
        ]
        scenes = []
        for i in range(n_scenes):
            beat = beats[i % len(beats)]
            narration = (
                f"{beat} "
                f"This is scene {i + 1} of the story, carrying the thread forward "
                f"with the weight of everything that came before."
            )
            scene_type = "video" if i in (0, n_scenes - 1) else "still"
            scenes.append(
                GeneratedScene(
                    narration_text=textwrap.shorten(narration, width=320, placeholder="..."),
                    image_prompt=(
                        f"Cinematic, dramatic illustration for a short-form video about "
                        f"{topic}. Scene {i + 1}: {beat} Rich lighting, painterly style, "
                        f"strong composition, vertical 9:16 framing."
                    ),
                    scene_type=scene_type,
                    # Spread the guessed cast across scenes so the character-sheet
                    # review step is exercised offline too.
                    characters=cast[: (1 if i % 2 == 0 else len(cast))] if cast else [],
                )
            )
        metadata = {
            "title": f"The Story of {topic.title()}",
            "description": (
                f"A short, cinematic retelling of {topic}. "
                f"(Generated offline — add API keys in .env for real script generation.)"
            ),
            "hashtags": ["#story", "#shorts", "#" + "".join(topic.title().split())[:24]],
            "suggested_caption": f"The Story of {topic.title()} — you won't believe how it ends. #story #shorts",
            "hook_text": f"What really happened with {topic}?",
        }
        return GeneratedScript(scenes=scenes, metadata=metadata)


_STOPWORDS = {"The", "A", "An", "And", "Of", "In", "On", "To", "With", "For", "But"}


def _guess_character_names(topic: str) -> list[str]:
    """Cheap proper-noun extraction for the offline generator's cast."""
    names = []
    for word in re.findall(r"[A-Z][a-zA-Z]+", topic):
        if word not in _STOPWORDS and word not in names:
            names.append(word)
    return names[:4]


def ai_character_description(name: str, topic: str, content_prompt: str) -> str:
    """One-line appearance description for a character reference sheet.

    Uses the active LLM provider; falls back to a sensible default offline.
    """
    instruction = (
        f"In 1-2 sentences, describe the visual appearance of the character "
        f'"{name}" as they would appear in a video about "{topic}". '
        f"Context/tone: {content_prompt[:400]}. "
        f"Focus only on look: physique, clothing, distinctive features, palette. "
        f"No preamble, just the description."
    )
    try:
        if settings.anthropic_api_key and settings.default_llm_provider != "openai":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.anthropic_script_model,
                max_tokens=200,
                messages=[{"role": "user", "content": instruction}],
            )
            return next((b.text for b in resp.content if b.type == "text"), "").strip()
        if settings.openai_api_key:
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_script_model,
                    "messages": [{"role": "user", "content": instruction}],
                    "max_tokens": 200,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:  # noqa: BLE001 — description is best-effort
        pass
    return f"{name}, a central figure in the story of {topic}."
