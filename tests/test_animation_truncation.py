"""Letting a long Manim program finish instead of cutting it off.

A long animation scene once overran the authoring call's output cap. The answer
came back cut off mid-statement (`infl1` with no assignment), which is still
valid Python, so it passed straight through to Manim and died there as a
NameError on the last line. Hitting the cap is no longer treated as an answer:
the partial is kept and the model is asked to carry on from where it stopped.
These tests pin that resume loop, the request shape it uses (Claude rejects a
request whose last message is an assistant turn, so the partial has to be
sandwiched), and the guards that still refuse an answer that never converges.
"""
import unittest
from unittest import mock

from backend.adapters import llm
from backend.animation.catalog import MAX_CODE_CHARS, normalize_spec

# The tail of the real failure: valid Python, but the program stops here.
TRUNCATED = 'title = Text("Lena")\nself.play_at(self.cue("Lena"), Write(title))\ninfl1'
GOOD = 'title = Text("Lena")\nself.play_at(self.cue("Lena"), Write(title))'


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeAnthropic:
    """Serves scripted (text, stop_reason) answers, repeating the last one."""

    answers: list[tuple[str, str]] = []
    calls: list[list[dict]] = []
    configs: list[dict | None] = []

    def __init__(self, api_key=None):
        self.messages = self

    def stream(self, model=None, max_tokens=None, messages=None, output_config=None):
        _FakeAnthropic.calls.append(messages)
        _FakeAnthropic.configs.append(output_config)
        text, stop_reason = (
            _FakeAnthropic.answers.pop(0)
            if len(_FakeAnthropic.answers) > 1
            else _FakeAnthropic.answers[0]
        )
        block = mock.Mock()
        block.type = "text"
        block.text = text
        return _FakeStream(mock.Mock(content=[block], stop_reason=stop_reason))


def _run(answers, fn):
    _FakeAnthropic.answers = list(answers)
    _FakeAnthropic.calls = []
    _FakeAnthropic.configs = []
    fake_module = mock.Mock(Anthropic=_FakeAnthropic)
    with mock.patch.dict("sys.modules", {"anthropic": fake_module}), \
            mock.patch.object(llm.settings, "anthropic_api_key", "test-key"), \
            mock.patch.object(llm.settings, "default_llm_provider", "anthropic"):
        return fn()


def _author(answers) -> str | None:
    return _run(
        answers,
        lambda: llm.author_manim_code("Lena ist vierundzwanzig", 35.0, True),
    )


def _prompts() -> list[str]:
    return [call[0]["content"] for call in _FakeAnthropic.calls]


class ContinuationTests(unittest.TestCase):
    def test_a_truncated_answer_is_continued_not_discarded(self):
        code = _author([("a = 1\n", "max_tokens"), ("b = 2", "end_turn")])
        self.assertEqual(code, "a = 1\nb = 2")
        self.assertEqual(len(_FakeAnthropic.calls), 2)

    def test_the_resume_request_never_ends_on_an_assistant_turn(self):
        # Claude 400s on a trailing assistant turn, so the partial answer is
        # sandwiched between the original ask and a continue instruction.
        _author([("a = 1\n", "max_tokens"), ("b = 2", "end_turn")])
        resume = _FakeAnthropic.calls[1]
        self.assertEqual([m["role"] for m in resume], ["user", "assistant", "user"])
        self.assertEqual(resume[1]["content"], "a = 1\n")
        self.assertIn("stopped at the output limit", resume[2]["content"])
        self.assertIn("Do not repeat any code", resume[2]["content"])

    def test_every_part_is_joined_in_spoken_order(self):
        code = _author([
            ("a = 1\n", "max_tokens"),
            ("b = 2\n", "max_tokens"),
            ("c = 3", "end_turn"),
        ])
        self.assertEqual(code, "a = 1\nb = 2\nc = 3")
        self.assertEqual(len(_FakeAnthropic.calls), 3)

    def test_a_fence_opened_in_the_first_part_is_stripped_once(self):
        # Stripping happens after joining, so a fence opened in one part and
        # closed in another is still removed rather than left in the code.
        code = _author([("```python\na = 1\n", "max_tokens"), ("b = 2\n```", "end_turn")])
        self.assertEqual(code, "a = 1\nb = 2")

    def test_an_endless_answer_gives_up_instead_of_looping(self):
        self.assertIsNone(_author([(TRUNCATED, "max_tokens")]))
        # Two attempts, each bounded by the resume limit.
        self.assertEqual(len(_FakeAnthropic.calls), 2 * (llm._MAX_CONTINUATIONS + 1))

    def test_a_complete_answer_costs_one_call(self):
        self.assertEqual(_author([(GOOD, "end_turn")]), GOOD)
        self.assertEqual(len(_FakeAnthropic.calls), 1)

    def test_unparseable_code_is_refused_and_retried(self):
        broken = 'title = Text("Lena"'  # never closed
        self.assertEqual(_author([(broken, "end_turn"), (GOOD, "end_turn")]), GOOD)
        self.assertNotIn("was unusable", _prompts()[0])
        self.assertIn("did not parse as valid Python", _prompts()[1])

    def test_the_retry_asks_for_validity_not_brevity(self):
        # Length is handled by continuing, so nothing in the authoring path
        # pressures the model into a smaller animation than the scene deserves.
        broken = 'title = Text("Lena"'
        _author([(broken, "end_turn"), (GOOD, "end_turn")])
        for prompt in _prompts():
            self.assertNotIn("SHORTER", prompt)
            self.assertNotIn("length limit", prompt.replace("no length limit", ""))

    def test_the_output_budget_is_the_models_ceiling(self):
        # 4000 tokens is what cut the 35-second scene off. The cap is a ceiling,
        # not a target, so it sits at what the model can actually emit.
        self.assertEqual(llm._ANTHROPIC_MAX_TOKENS, 128000)


class EffortTests(unittest.TestCase):
    """Opus 5 thinks on every call and bills it, so effort is sized per task."""

    def test_authoring_asks_for_high_effort(self):
        _author([(GOOD, "end_turn")])
        self.assertEqual(_FakeAnthropic.configs[0], {"effort": "high"})

    def test_a_resume_keeps_the_same_effort(self):
        _author([("a = 1\n", "max_tokens"), ("b = 2", "end_turn")])
        self.assertEqual(_FakeAnthropic.configs, [{"effort": "high"}] * 2)

    def test_the_ladder_never_exceeds_high(self):
        # xhigh and max buy diminishing returns for this pipeline; the ceiling
        # is deliberate, not an oversight.
        ladder = [llm._EFFORT_AUTHORING, llm._EFFORT_PLANNING, llm._EFFORT_PHRASING]
        self.assertEqual(ladder, ["high", "medium", "low"])
        for level in ladder:
            self.assertNotIn(level, ("xhigh", "max"))


class RepairTests(unittest.TestCase):
    def test_repair_continues_the_same_way(self):
        code = _run(
            [("a = 1\n", "max_tokens"), ("b = 2", "end_turn")],
            lambda: llm.repair_manim_code(
                TRUNCATED, "NameError: name 'infl1' is not defined",
                "Lena ist vierundzwanzig", 35.0, True,
            ),
        )
        self.assertEqual(code, "a = 1\nb = 2")
        # The repair prompt carries the failed program and its error, and asks
        # for a fix — not for a shorter rewrite.
        self.assertIn(TRUNCATED, _prompts()[0])
        self.assertIn("NameError", _prompts()[0])


class SpecLimitTests(unittest.TestCase):
    def test_over_long_code_is_rejected_not_trimmed(self):
        # Trimming would produce exactly the truncated program this all guards
        # against, so the spec is dropped instead.
        self.assertIsNone(normalize_spec({"code": "a = 1\n" * MAX_CODE_CHARS, "title": ""}))

    def test_normal_code_still_passes(self):
        self.assertEqual(normalize_spec({"code": GOOD, "title": "Lena"})["code"], GOOD)


if __name__ == "__main__":
    unittest.main()
