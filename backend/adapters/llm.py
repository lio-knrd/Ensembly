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

# How hard the model should think, per task. Opus 5 thinks on every request and
# bills that thinking as output, so leaving every call at the API default of
# "high" pays deep reasoning for naming a character's clothes. The ladder below
# sizes it to the job and deliberately stops at "high": xhigh and max buy
# diminishing returns here, and nothing in this pipeline is worth them.
_EFFORT_AUTHORING = "high"    # writing a script or a Manim program
_EFFORT_PLANNING = "medium"   # judgement calls over a topic: scope, series split
_EFFORT_PHRASING = "low"      # a sentence or two of description or house style
# Only the Anthropic calls carry it; the OpenAI paths have no such parameter.


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
        if st not in ("still", "video", "animation"):
            st = "still"
        # The script LLM no longer authors Manim code: an animation scene only
        # declares its scene_type here. The code is generated later, after audio
        # exists, from the (merged) narration — so animation stays None at this
        # stage. See pipeline._render_scene_animation / llm.author_manim_code.
        animation = None
        continuity_context = []
        for item in raw.get("continuity_context", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                source_scene = int(item.get("source_scene", 0))
            except (TypeError, ValueError):
                source_scene = 0
            visual_anchor = str(item.get("visual_anchor", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if source_scene > 0 and visual_anchor:
                continuity_context.append({
                    "source_scene": source_scene,
                    "visual_anchor": visual_anchor,
                    "reason": reason,
                })
        characters = []
        for item in raw.get("characters", []) or []:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                characters.append({
                    "name": name,
                    "state": str(item.get("state", "") or "").strip(),
                    "state_importance": str(item.get("state_importance", "default") or "default").strip(),
                    "state_notes": str(item.get("state_notes", "") or "").strip(),
                })
            else:
                name = str(item).strip()
                if name:
                    characters.append({
                        "name": name,
                        "state": "",
                        "state_importance": "default",
                        "state_notes": "",
                    })
        scenes.append(
            GeneratedScene(
                narration_text=str(raw.get("narration_text", "")).strip(),
                image_prompt=str(raw.get("image_prompt", "")).strip(),
                scene_type=st,
                characters=characters,
                continuity_context=continuity_context,
                animation=animation,
            )
        )
    return GeneratedScript(scenes=scenes, metadata=data.get("metadata", {}))


class AnthropicScriptGenerator(ScriptGenerator):
    name = "anthropic"

    def generate(self, system_prompt, user_prompt, schema, topic, target_duration_seconds):
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        # Streamed: a full scene list plus the model's thinking needs a budget
        # past what a non-streaming request may hold open, and a script cut off
        # mid-JSON fails the whole generation rather than degrading.
        with client.messages.stream(
            model=settings.anthropic_script_model,
            max_tokens=64000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={
                "format": {"type": "json_schema", "schema": schema},
                "effort": _EFFORT_AUTHORING,
            },
        ) as stream:
            response = stream.get_final_message()
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
                # Not strict: the animation-enabled schema uses optional fields
                # and free-form params. _coerce_script sanitizes the output.
                "type": "json_schema",
                "json_schema": {"name": "script", "schema": schema, "strict": False},
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
                        f"Short-form video scene about {topic}. Scene {i + 1}: {beat} "
                        f"Clear focal subject, dramatic moment, strong composition, "
                        f"expressive lighting, vertical 9:16 framing."
                    ),
                    scene_type=scene_type,
                    # Spread the guessed cast across scenes so the character-sheet
                    # review step is exercised offline too.
                    characters=[
                        {
                            "name": name,
                            "state": "",
                            "state_importance": "default",
                            "state_notes": "",
                        }
                        for name in (cast[: (1 if i % 2 == 0 else len(cast))] if cast else [])
                    ],
                    continuity_context=[],
                )
            )
        metadata = {
            "title": f"The Story of {topic.title()}",
            "description": (
                f"A short, cinematic retelling of {topic}. "
                f"(Generated offline — add API keys in .env for real script generation.)"
            ),
            "hashtags": ["#story", "#shorts", "#" + "".join(topic.title().split())[:24]],
            "hook_text": f"What really happened with {topic}?",
            "cover_kicker": "THE UNTOLD STORY",
            "cover_title": textwrap.shorten(topic.title(), width=28, placeholder=""),
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


SCOPE_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "split_recommended": {"type": "boolean"},
        "reason": {"type": "string"},
        "suggested_title": {"type": "string"},
        "series_throughline": {"type": "string"},
        "part_1": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"},
                "ending_boundary": {"type": "string"},
            },
            "required": ["focus", "ending_boundary"],
            "additionalProperties": False,
        },
        "part_2": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"},
                "opening_bridge": {"type": "string"},
            },
            "required": ["focus", "opening_bridge"],
            "additionalProperties": False,
        },
    },
    "required": [
        "split_recommended", "reason", "suggested_title", "series_throughline",
        "part_1", "part_2",
    ],
    "additionalProperties": False,
}


def analyze_project_scope(
    topic: str,
    title: str,
    target_duration_seconds: int,
    platform_prompt: str = "",
    content_prompt: str = "",
) -> dict:
    """Decide whether one focused video can cover the topic without rushing."""
    instruction = f"""\
Assess whether this topic can become one coherent narrated short-form video of
about {target_duration_seconds} seconds (roughly {round(target_duration_seconds * 2.5)} spoken words).

Recommend two parts ONLY when a single video would have to omit essential causal
steps, compress distinct major arcs into a list, or become confusing. Do not
split merely because the wider subject has more detail available: a focused,
complete angle is preferable when it preserves the user's core intent.

If a split is recommended, design exactly two complementary parts. Choose a
natural narrative or explanatory hinge: Part 1 must feel satisfying while ending
at that hinge; Part 2 must start just after it, use at most one brief bridging
sentence, and never retell Part 1. Together they must cover the essential arc
without gaps, premature cutoff, padding, or duplicated explanation.

Always suggest a concise project/series title, even if no title was supplied.
Do not inspect or account for existing project titles; uniqueness is handled by
the application.

Supplied title: {title.strip() or "(none)"}
Topic / idea: {topic.strip()}
Platform guidance: {platform_prompt.strip() or "(none)"}
Content guidance: {content_prompt.strip() or "(none)"}

Return only the structured assessment."""
    try:
        if settings.anthropic_api_key and settings.default_llm_provider != "openai":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = client.messages.create(
                model=settings.anthropic_script_model,
                max_tokens=16000,
                messages=[{"role": "user", "content": instruction}],
                output_config={
                    "format": {"type": "json_schema", "schema": SCOPE_ANALYSIS_SCHEMA},
                    "effort": _EFFORT_PLANNING,
                },
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            return _extract_json(text)
        if settings.openai_api_key:
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_script_model,
                    "messages": [{"role": "user", "content": instruction}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "project_scope",
                            "schema": SCOPE_ANALYSIS_SCHEMA,
                            "strict": True,
                        },
                    },
                },
                timeout=120,
            )
            response.raise_for_status()
            return _extract_json(response.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 - creation can still proceed with a safe fallback
        pass
    return _offline_scope_analysis(topic, title, target_duration_seconds)


def _offline_scope_analysis(topic: str, title: str, target_duration_seconds: int) -> dict:
    words = topic.split()
    arc_markers = len(re.findall(r"[;,]|\b(?:then|after|before|rise|fall|and finally)\b", topic, re.I))
    split = len(words) > max(45, int(target_duration_seconds * 0.55)) and arc_markers >= 2
    base_title = title.strip() or textwrap.shorten(topic.strip(), width=72, placeholder="…") or "Untitled"
    midpoint = max(1, len(words) // 2)
    return {
        "split_recommended": split,
        "reason": (
            "The topic contains multiple major steps that would be rushed at the selected duration."
            if split else
            "The topic can be shaped into one focused video at the selected duration."
        ),
        "suggested_title": base_title,
        "series_throughline": topic.strip(),
        "part_1": {
            "focus": " ".join(words[:midpoint]) if split else topic.strip(),
            "ending_boundary": "End at the central turning point; do not begin the later consequences.",
        },
        "part_2": {
            "focus": " ".join(words[midpoint:]) if split else "",
            "opening_bridge": "Open immediately after Part 1's turning point with no extended recap.",
        },
    }


EDITORIAL_SUGGESTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "coverage_scope": {"type": "string"},
                    "rationale": {"type": "string"},
                    "part_group_title": {"type": "string"},
                    "part_number": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                    },
                },
                "required": [
                    "title", "summary", "coverage_scope", "rationale",
                    "part_group_title", "part_number",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overview", "suggestions"],
    "additionalProperties": False,
}


def suggest_editorial_items(
    *,
    plan_name: str,
    description: str,
    editorial_rules: str,
    ordering_mode: str,
    platform_prompt: str,
    content_prompt: str,
    existing_items: list[dict],
    instruction: str,
    count: int,
) -> dict:
    """Suggest the next works using compact coverage records, not full scripts."""
    history = "\n".join(
        (
            f"{index + 1}. [{item.get('status', 'planned')}] {item.get('title', '')}\n"
            f"   Summary: {item.get('summary', '') or '(none)'}\n"
            f"   Covered: {item.get('coverage_summary', '') or '(not documented)'}\n"
            f"   Parts: {item.get('part_group_title', '') or '-'} "
            f"{item.get('part_number') or ''}"
        )
        for index, item in enumerate(existing_items)
    ) or "(No works recorded yet.)"
    prompt = f"""\
You are an editorial planner. Propose the next {count} works for a general
content series. Respect the plan's own ordering rules; do not assume chronology
unless the rules request it.

Plan: {plan_name}
Purpose: {description or "(none)"}
Ordering mode: {ordering_mode}
Editorial rules:
{editorial_rules or "(none)"}

Platform guidance:
{platform_prompt or "(none)"}

Content guidance:
{content_prompt or "(none)"}

Works already suggested, planned, completed, published, skipped, or externally produced:
{history}

User request:
{instruction.strip()}

Avoid duplicating covered material. You may make a brief callback to prior work,
but do not pitch it as new coverage. Find genuine gaps and logical next steps.
If one proposed work cannot fit a normal short-form video without omitting
essential steps, split only that work into exactly two consecutive suggestions.
Give both the same non-empty part_group_title and part_number 1 and 2. Otherwise
part_group_title must be empty and part_number null. Each coverage_scope must
state precise start/end boundaries and what is deliberately left for later.
Return only the structured response."""
    try:
        if settings.anthropic_api_key and settings.default_llm_provider != "openai":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = client.messages.create(
                model=settings.anthropic_script_model,
                max_tokens=16000,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": EDITORIAL_SUGGESTIONS_SCHEMA},
                    "effort": _EFFORT_PLANNING,
                },
            )
            text = next((b.text for b in response.content if b.type == "text"), "")
            return _normalize_editorial_suggestions(_extract_json(text), count)
        if settings.openai_api_key:
            response = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_script_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "editorial_suggestions",
                            "schema": EDITORIAL_SUGGESTIONS_SCHEMA,
                            "strict": True,
                        },
                    },
                },
                timeout=120,
            )
            response.raise_for_status()
            return _normalize_editorial_suggestions(
                _extract_json(response.json()["choices"][0]["message"]["content"]),
                count,
            )
    except Exception:  # noqa: BLE001 - offline planning remains available
        pass
    return _offline_editorial_suggestions(instruction, existing_items, count)


def _normalize_editorial_suggestions(data: dict, count: int) -> dict:
    suggestions = []
    for raw in (data.get("suggestions") or [])[:max(1, min(count, 10))]:
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        part_number = raw.get("part_number")
        try:
            part_number = int(part_number) if part_number is not None else None
        except (TypeError, ValueError):
            part_number = None
        suggestions.append({
            "title": title[:120],
            "summary": str(raw.get("summary", "")).strip(),
            "coverage_scope": str(raw.get("coverage_scope", "")).strip(),
            "rationale": str(raw.get("rationale", "")).strip(),
            "part_group_title": str(raw.get("part_group_title", "")).strip(),
            "part_number": part_number if part_number in (1, 2) else None,
        })
    return {"overview": str(data.get("overview", "")).strip(), "suggestions": suggestions}


def _offline_editorial_suggestions(instruction: str, existing_items: list[dict], count: int) -> dict:
    start = len(existing_items) + 1
    subject = textwrap.shorten(instruction.strip(), width=70, placeholder="…") or "Next topic"
    suggestions = [
        {
            "title": f"{subject} — Idea {start + index}",
            "summary": f"A focused next entry responding to: {subject}",
            "coverage_scope": "Define the precise coverage boundary before production.",
            "rationale": "Offline placeholder; configure an LLM provider for context-aware planning.",
            "part_group_title": "",
            "part_number": None,
        }
        for index in range(max(1, min(count, 10)))
    ]
    return {
        "overview": "Offline planning suggestions generated without an LLM provider.",
        "suggestions": suggestions,
    }


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


# max_tokens is a ceiling, not a target: the model stops when the program is
# done, exactly like a chat reply ends when the answer ends. The old 4000 was
# simply too low a ceiling for a 35-second scene, so answers were cut off
# mid-statement — and a cut-off program often still parses (a dangling `infl1`
# is a valid expression), so it reached the renderer and died as a NameError.
# Claude tops out at 128k output tokens, far beyond any construct body, which
# effectively removes truncation here. Requests that large must stream: the SDK
# refuses a non-streaming call it expects to outlive the connection.
_ANTHROPIC_MAX_TOKENS = 128000
# GPT-4o's output ceiling is much lower, so that provider leans on the
# continuation loop below rather than on a single large budget.
_OPENAI_MAX_TOKENS = 16000
# Resumes allowed before giving up. A construct body that is still unfinished
# after this many is not going to converge.
_MAX_CONTINUATIONS = 4

# Claude rejects a request whose LAST message is an assistant turn, so a partial
# answer cannot simply be handed back to be extended. It is instead sandwiched
# between the original ask and this note, which is the supported way to resume.
_CONTINUE_NOTE = """\
Your previous message stopped at the output limit, mid-program. Continue the \
construct body from exactly where it stopped. Do not repeat any code you already \
wrote, do not restate the whole program, do not open a markdown fence, and do not \
add commentary. If your last line was left unfinished, complete that line first."""

_RETRY_NOTE = """

IMPORTANT — your previous answer was unusable: it did not parse as valid Python, \
or it never reached the end of the program. Write the animation again as one \
complete, syntactically valid construct body. Every statement must be finished \
and every name defined before it is used."""


def _compiles(code: str) -> bool:
    """Does this construct body parse as Python at all?

    Cheap guard against a mangled or half-written answer reaching Manim, where
    the same problem surfaces as an opaque traceback three subprocesses deep.
    """
    try:
        compile(code, "<animation>", "exec")
    except (SyntaxError, ValueError):
        return False
    return True


def _complete_code(messages: list[dict]) -> tuple[str | None, bool]:
    """One code completion against the active provider.

    Returns ``(text, truncated)``, where ``truncated`` means the model hit the
    output cap and the answer therefore stops mid-program. ``(None, False)``
    means no provider is configured. The text is returned raw — fences are
    stripped once, after any continuations have been joined.
    """
    if settings.anthropic_api_key and settings.default_llm_provider != "openai":
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        with client.messages.stream(
            model=settings.anthropic_script_model,
            max_tokens=_ANTHROPIC_MAX_TOKENS,
            messages=messages,
            # Layout, cue ordering and on-screen arithmetic all have to be right
            # at once — this is the hardest call in the pipeline.
            output_config={"effort": _EFFORT_AUTHORING},
        ) as stream:
            resp = stream.get_final_message()
        text = next((b.text for b in resp.content if b.type == "text"), "")
        truncated = getattr(resp, "stop_reason", "") == "max_tokens"
        return text or None, truncated
    if settings.openai_api_key:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            json={
                "model": settings.openai_script_model,
                "messages": messages,
                "max_tokens": _OPENAI_MAX_TOKENS,
            },
            timeout=180,
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
        return choice["message"]["content"] or None, choice.get("finish_reason") == "length"
    return None, False


def _complete_until_done(instruction: str) -> str | None:
    """Keep the model going until it ends the program on its own.

    Hitting the output cap is not treated as an answer any more: the partial is
    kept and the model is asked to carry on from where it stopped, so length
    stops being a failure mode instead of merely a bigger budget. Returns None
    when no provider is configured, a call fails, or the program is still
    unfinished after ``_MAX_CONTINUATIONS`` resumes.
    """
    messages: list[dict] = [{"role": "user", "content": instruction}]
    parts: list[str] = []
    for _ in range(_MAX_CONTINUATIONS + 1):
        try:
            text, truncated = _complete_code(messages)
        except Exception:  # noqa: BLE001 — authoring stays best-effort
            return None
        if text is None:
            return None
        parts.append(text)
        if not truncated:
            return _strip_code_fences("".join(parts)) or None
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": "".join(parts)},
            {"role": "user", "content": _CONTINUE_NOTE},
        ]
    return None


def _authored_code(instruction: str) -> str | None:
    """Complete, syntactically valid construct-body code, or None.

    Runs the completion (resuming as often as the program needs) and retries
    once when the result is still unfinished or unparseable. Returning None makes
    the caller fail loudly, which is strictly better than rendering a half
    program.
    """
    for prompt in (instruction, instruction + _RETRY_NOTE):
        code = _complete_until_done(prompt)
        # Only an unusable answer earns the second, shorter-prompt attempt. With
        # no provider configured the retry costs nothing — it makes no call.
        if code and _compiles(code):
            return code
    return None


def author_manim_code(
    narration: str,
    duration: float,
    latex_available: bool,
    style_prompt: str = "",
    revision_notes: str = "",
) -> str | None:
    """Author Manim construct-body code for one animation scene, AFTER its audio.

    Called in the storyboard stage once narration audio + word timestamps exist,
    so the diagram is written against the final (merged) narration and its real
    length. Voice sync stays phrase-anchored via ``self.cue(...)``, which resolves
    the injected WORDS at render time. Returns construct-body code (no fences), or
    None if no LLM is available. The pipeline then renders it with the same
    bounded auto-repair loop used for user-edited code.

    ``style_prompt`` is the content preset's ``animation_style_prompt`` — the
    group's house style for diagrams (palette, type sizes, layout). It is placed
    AFTER the catalog so a group can tighten the catalog's defaults (which offer
    a whole spectrum of colors) down to its own, but never override the hard
    constraints above it. Empty for groups that have not set one, in which case
    the prompt is byte-identical to before.

    ``revision_notes`` is the project's creator feedback (``Project.revision_notes``).
    It lands after the house style because it is a correction of what this very
    project got wrong last time, and is empty for projects without notes.
    """
    from ..animation.catalog import catalog_for_prompt

    style_block = ""
    if style_prompt.strip():
        style_block = (
            "\nHouse style for this channel — follow it exactly, and prefer it over "
            "any conflicting stylistic suggestion in the rules above (it does not "
            "override the canvas, sync, or safety constraints):\n"
            f"{style_prompt.strip()}\n"
        )
    if revision_notes.strip():
        style_block += (
            "\nCorrection notes from the creator for this project — an earlier "
            "version was rejected. Apply them to this diagram wherever they touch "
            "what it shows (they do not override the canvas, sync, or safety "
            "constraints):\n"
            f"{revision_notes.strip()}\n"
        )

    instruction = f"""Write Manim Community code — the BODY of construct(self) — for a short \
vertical (9:16) animation that visualizes the narration below and stays in sync \
with the spoken voice. Return ONLY the construct body: no imports, no class line, \
no def line, no markdown fences.

Authoring rules you must follow:
{catalog_for_prompt(latex_available)}
{style_block}
Scene narration (spoken over this animation — copy exact phrases into self.cue): {narration}
Target duration: {duration:.1f} seconds (pace the animation to fill it).

Return only the construct-body code."""
    return _authored_code(instruction)


def repair_manim_code(
    code: str,
    error: str,
    narration: str,
    duration: float,
    latex_available: bool,
) -> str | None:
    """Ask the LLM to fix Manim construct-body code that failed to render.

    Returns corrected code (no fences), or None if no LLM is available. Used by
    the pipeline's bounded auto-repair loop for animation scenes.
    """
    from ..animation.catalog import catalog_for_prompt

    instruction = f"""You authored Manim Community code — the body of construct(self) for a short \
vertical (9:16) animation — but it FAILED to render. Fix it. Return ONLY the \
corrected construct body: no imports, no class line, no def line, no markdown fences.

Authoring rules you must follow:
{catalog_for_prompt(latex_available)}

Scene narration (use exact phrases for self.cue): {narration}
Target duration: {duration:.1f} seconds

--- The code that failed ---
{code}

--- The Manim / Python error ---
{error}

Return only the corrected construct-body code."""
    return _authored_code(instruction)


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
                # The description is one or two sentences; the budget is sized
                # for the thinking that precedes it, not for the answer.
                max_tokens=16000,
                messages=[{"role": "user", "content": instruction}],
                output_config={"effort": _EFFORT_PHRASING},
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


def ai_image_style_prompt(content_prompt: str, current_style_prompt: str = "", guidelines: str = "") -> str:
    """Generate a reusable image style directive for character-consistent visuals."""
    instruction = (
        "Write a concise image style prompt for an AI video/image pipeline. "
        "It will be appended to every character sheet and every scene image prompt. "
        "Base it on the story/content context and the user's optional guidance. "
        "The prompt must protect character consistency: define one coherent visual "
        "language, one consistent character embodiment taxonomy, and rules that keep "
        "each character's species/body plan, age category, silhouette, clothing logic, "
        "palette, facial structure, and proportions stable across scenes. "
        "If animals, creatures, robots, humans, or stylized beings appear, prevent "
        "mixed interpretations such as some characters becoming humanoid while others "
        "remain naturalistic unless the user explicitly asks for that contrast. "
        "Do not invent plot events. Do not include camera directions, scene-specific "
        "actions, or character names. Return only the finished style prompt, no preamble.\n\n"
        f"Content prompt:\n{content_prompt[:2000] or '(empty)'}\n\n"
        f"Existing style prompt to improve or replace:\n{current_style_prompt[:1200] or '(empty)'}\n\n"
        f"Additional user guidance:\n{guidelines[:1200] or '(none)'}"
    )
    try:
        if settings.anthropic_api_key and settings.default_llm_provider != "openai":
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            resp = client.messages.create(
                model=settings.anthropic_script_model,
                # Short prose out, but thinking is billed against the same cap.
                max_tokens=16000,
                messages=[{"role": "user", "content": instruction}],
                output_config={"effort": _EFFORT_PHRASING},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "").strip()
            if text:
                return _clean_style_prompt(text)
        if settings.openai_api_key:
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_script_model,
                    "messages": [{"role": "user", "content": instruction}],
                    "max_tokens": 450,
                },
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            if text:
                return _clean_style_prompt(text)
    except Exception:  # noqa: BLE001 - style suggestions are best-effort
        pass
    return _offline_style_prompt(content_prompt, current_style_prompt, guidelines)


def _clean_style_prompt(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(r"^(style prompt|image style prompt)\s*:\s*", "", text, flags=re.I).strip()
    return textwrap.shorten(text, width=1400, placeholder="...")


def _offline_style_prompt(content_prompt: str, current_style_prompt: str, guidelines: str) -> str:
    base = current_style_prompt.strip() or guidelines.strip()
    if not base:
        base = "Cohesive cinematic illustration with clear silhouettes, expressive lighting, balanced detail, and a unified color palette."
    context_note = ""
    if content_prompt.strip():
        context_note = " Match the subject matter and tone of the content prompt without adding scene-specific actions."
    consistency = (
        " Character consistency is mandatory: keep every recurring character's species or archetype, body plan, proportions, "
        "age category, facial structure, silhouette, clothing logic, and core palette stable across character sheets and scenes. "
        "Use one consistent embodiment taxonomy for all comparable characters; do not mix naturalistic, humanoid, cartoon, "
        "mechanical, or creature interpretations unless the prompt explicitly requests that contrast."
    )
    return textwrap.shorten(f"{base}{context_note}{consistency}", width=1400, placeholder="...")
