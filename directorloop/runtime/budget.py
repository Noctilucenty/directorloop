"""Run limits enforced at the provider boundary: a model-call ceiling and a wall-clock deadline.

Every judge_json / complete_json / answer_questions call is charged before it is sent. When a limit is reached the call
raises BudgetExceeded, which the runners turn into an explicit stop state. Token usage is recorded from provider replies;
money is not estimated because no verified price table is configured (unknown stays unknown).
"""

from __future__ import annotations

import threading
import time
from typing import Any


class BudgetExceeded(RuntimeError):
    pass


class CallBudget:
    def __init__(self, max_calls: int | None = None, deadline_s: float | None = None) -> None:
        self.max_calls = max_calls
        self.deadline_s = deadline_s
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    def check_deadline(self) -> None:
        if self.deadline_s is not None and self.elapsed_s() > self.deadline_s:
            raise BudgetExceeded(f"deadline of {self.deadline_s:.0f} s reached after {self.elapsed_s():.0f} s")

    def charge(self) -> None:
        with self._lock:
            self.check_deadline()
            if self.max_calls is not None and self.calls >= self.max_calls:
                raise BudgetExceeded(f"model-call limit of {self.max_calls} reached")
            self.calls += 1

    def record_usage(self, result: Any) -> None:
        with self._lock:
            self.input_tokens += int(getattr(result, "input_tokens", None) or 0)
            self.output_tokens += int(getattr(result, "output_tokens", None) or 0)

    def summary(self) -> dict[str, Any]:
        return {"model_calls": self.calls, "max_model_calls": self.max_calls, "deadline_s": self.deadline_s, "elapsed_s": round(self.elapsed_s(), 1),
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "cost_usd": None,
                "cost_note": "not estimated: no verified price table is configured"}


class BudgetedProvider:
    """Wraps a provider; exposes the same capability and charges the budget per request."""

    def __init__(self, inner: Any, budget: CallBudget) -> None:
        self._inner = inner
        self._budget = budget
        self.capability = inner.capability
        self.reasoning_effort = getattr(inner, "reasoning_effort", None)
        self.is_fake = getattr(inner, "is_fake", False)

    def judge_json(self, media: Any, instruction: str, schema: dict[str, Any]) -> Any:
        self._budget.charge()
        res = self._inner.judge_json(media, instruction, schema)
        self._budget.record_usage(res)
        return res

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *, temperature: float = 0.2) -> Any:
        self._budget.charge()
        res = self._inner.complete_json(system, user, schema, temperature=temperature)
        self._budget.record_usage(res)
        return res

    def answer_questions(self, media: Any, questions: list[Any], *, seed: int) -> Any:
        self._budget.charge()
        return self._inner.answer_questions(media, questions, seed=seed)
