"""Synthetic response functions, not vision processing or model-quality evidence.

Only request inputs are visible here. The adapter never reads expected/shipped
floor data. Quotes and measured usage are synthetic, nonzero, and dollar-bound.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from pydantic import JsonValue

from architect.models import FakeAdapter, ModelRequest, ModelResponse, UsageQuote

VISION = "gpt-4o-mini"
SOLVER = "llama3.2"
VERIFIER = "claude-sonnet-4-20250514"
PIPELINE_MODELS = (VISION, SOLVER, VERIFIER)


def draft_report(task: dict) -> str:
    return (
        f"Completed {task['completed']} of {task['assigned']} {task['topic']} activities."
        "\n\nNext, review the remaining activities with a teacher."
    )


def rederive(questions: list) -> list[dict]:
    # A deliberately tiny arithmetic fixture, not a general solver. Do not read
    # printed_answer: re-derive from the synthetic expression independently.
    answers = []
    for question in questions:
        left, operator, right = question["expression"].split()
        if operator != "+":
            raise ValueError("unsupported_synthetic_expression")
        answers.append({"id": question["id"], "answer": int(left) + int(right)})
    return answers


class SyntheticAdapter(FakeAdapter):
    def __init__(self, model: str, respond: Callable[[dict], JsonValue], counts: Counter):
        super().__init__(model=model)
        self.respond = respond
        self.counts = counts

    def _response(self, request: ModelRequest) -> ModelResponse:
        payload = json.loads(request.prompt)
        value = self.respond(payload)
        text = value if isinstance(value, str) else json.dumps(value)
        return ModelResponse(
            text=text, model=self.model, tokens_in=80, tokens_out=40, cost_usd=0.00012,
        )

    def quote(self, request: ModelRequest) -> UsageQuote:
        response = self._response(request)
        return UsageQuote(
            tokens_in=response.tokens_in, tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.counts[self.model] += 1
        return self._response(request)


def adapter_factory(name: str, counts: Counter, *, fail_floor: bool = False):
    def factory(model: str) -> SyntheticAdapter:
        def respond(payload: dict) -> JsonValue:
            if payload.get("operation") == "verify_candidate":
                return {"passed": True, "reason": "synthetic-self-check"}
            task = payload["input"]
            if name == "report-cards":
                return "Incorrect synthetic draft." if fail_floor else draft_report(task)
            if name == "content-recommender":
                # Read the preset's explicit retrieval/preference context. No
                # floor labels, private client history, or external service.
                candidates = task.get("retrieved", [])
                preferences = task.get("preference_memory", {}).get("preferences", {})
                chosen = next((item["id"] for item in candidates
                               if item["topic"] == "fractions"
                               and preferences.get("format") == "practice"), "no-match")
                return {"content_id": "wrong-content" if fail_floor else chosen,
                        "review_required": True}
            if model == VISION:
                if task.get("synthetic") is not True:
                    raise ValueError("synthetic_only")
                return {"questions": task["questions"],
                        "layout_source": "synthetic-structured-page"}
            if model == SOLVER:
                if "vision-extract" not in task["steps"]:
                    raise ValueError("missing_vision_stage")
                # Use original expressions, NOT extracted printed keys.
                return {"answer_keys": rederive(task["original"]["questions"])}
            if model == VERIFIER:
                steps = task["steps"]
                extracted = steps["vision-extract"]["questions"]
                derived = steps["solver-rederive"]["answer_keys"]
                printed = {q["id"]: q["printed_answer"] for q in extracted}
                disagreements = [q["id"] for q in derived if printed[q["id"]] != q["answer"]]
                if fail_floor:
                    # Valid schema, deliberately wrong key: the real checker,
                    # not a scripted self-verdict, must reject this artifact.
                    derived = [{**q, "answer": q["answer"] + 1} for q in derived]
                return {"answer_keys": derived, "disagreements": disagreements,
                        "review_required": True}
            raise ValueError("unconfigured_synthetic_model")
        return SyntheticAdapter(model, respond, counts)
    return factory
