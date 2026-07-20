"""AWAITING-input classification (DESIGN §11).

Layer 1 — structural terminal facts (echo-off, alternate screen) — lives in
`Machine._classify`: it is mechanical and free. This module holds the protocol
and the optional layer 2: a small fast LLM judging everything structure can't
see. There are no pattern lists anywhere (§18.6).
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Literal, Protocol, Union, runtime_checkable

Verdict = Literal["awaiting", "working", "odd_exit"]

DEFAULT_MODEL = "anthropic:claude-haiku-4-5-20251001"


@runtime_checkable
class InputStateClassifier(Protocol):
    async def classify(
        self, tail_text: str, seconds_quiet: float, screen_text: str
    ) -> Verdict: ...


class LLMClassifier:
    """Judges ambiguous quiet periods with a small fast model.

    `model` is a pydantic-ai model name (default: Haiku — fast and cheap enough
    to call once per quiet period) or any (a)sync callable(prompt) -> str whose
    reply contains one of awaiting/working/odd_exit. The Machine caches
    verdicts per (console, cursor offset) so this fires at most once per quiet
    period. Any failure degrades to "working" — never let classification break
    the harness.
    """

    def __init__(
        self,
        model: Union[str, Callable[[str], Union[str, Awaitable[str]]]] = DEFAULT_MODEL,
        instructions: str | None = None,
    ) -> None:
        self._model = model
        self._agent = None
        self._instructions = instructions or (
            "You are watching one terminal of a developer's machine. Decide "
            "whether the foreground program is blocked waiting for the user to "
            "type something (awaiting), still working (working), or has ended "
            "oddly (odd_exit). Answer with exactly one word."
        )

    async def classify(
        self, tail_text: str, seconds_quiet: float, screen_text: str
    ) -> Verdict:
        tail = "\n".join(tail_text.splitlines()[-30:])
        prompt = (
            f"{self._instructions}\n\n"
            f"Quiet for {seconds_quiet:.1f}s.\n"
            f"--- last output ---\n{tail}\n"
            f"--- visible screen ---\n{screen_text}\n"
        )
        try:
            raw = (await self._invoke(prompt)).strip().lower()
        except Exception:
            return "working"
        for verdict in ("awaiting", "odd_exit", "working"):
            if verdict in raw:
                return verdict  # type: ignore[return-value]
        return "working"

    async def _invoke(self, prompt: str) -> str:
        if callable(self._model):
            result = self._model(prompt)
            if inspect.isawaitable(result):
                result = await result
            return str(result)
        if self._agent is None:
            try:
                from pydantic_ai import Agent
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "LLMClassifier with a model name needs pydantic-ai: "
                    "pip install 'altf[llm-classifier]'"
                ) from exc
            self._agent = Agent(self._model, output_type=str)
        result = await self._agent.run(prompt)
        return str(result.output)
