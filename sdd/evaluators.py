"""Providers return evidence, never SQL, permissions, or authoritative labels."""

from dataclasses import dataclass, field
import math
import httpx


@dataclass
class Evaluation:
    probability: float
    responder: str
    usage: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)

    def validate(self, expected_model):
        if isinstance(self.probability, bool) or not isinstance(self.probability, (int, float)):
            raise ValueError("Probability must be numeric")
        if not math.isfinite(self.probability) or not 0 <= self.probability <= 1:
            raise ValueError("Invalid probability")
        if self.responder != expected_model:
            raise ValueError("Responder differs from pinned evaluator model")
        return self


class ProviderError(RuntimeError):
    """Sanitized failure metadata; never retain the provider response body."""

    def __init__(self, code, retryable, retry_after=None):
        super().__init__(code)
        self.code, self.retryable = code, retryable
        self.retry_after = retry_after


class JevBackend:
    def __init__(self, api_key, client=None):
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY is required")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10))

    def evaluate(self, version, concept, evaluator):
        context = {k: version["context"][k] for k in concept["context_fields"]}
        question = {
            "type": "noul",
            "instructions": evaluator["instructions"] + "\n" + concept["definition"],
        }
        if concept["inclusion"] or concept["exclusion"]:
            question["criteria"] = {
                "true": concept["inclusion"] or concept["definition"],
                "false": concept["exclusion"] or "The definition does not apply.",
            }
        response = self.client.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": evaluator["model"],
                "state": {"message": version["text"], "context": context},
                "questions": {"predicate": question},
            },
        )
        if not response.is_success:
            code = response.status_code
            raise ProviderError(f"HTTP_{code}", code in (408, 429) or code >= 500)
        try:
            payload = response.json()
            answer = payload["answers"]["predicate"]
            if answer["type"] != "noul":
                raise ValueError("Expected Noul")
            usage = payload.get("usage", {})
            if not isinstance(usage, dict):
                raise ValueError("Invalid usage")
            if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in usage.values()):
                raise ValueError("Invalid token usage")
            return Evaluation(
                answer["noul"],
                payload["model"],
                usage,
                [{"source_version": version["id"], "scope": "whole_message"}],
            ).validate(evaluator["model"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("InvalidResponse", False) from exc


class FixtureBackend:
    """Explicit synthetic-test backend, never a substitute for Jev classification."""

    def __init__(self, probabilities=None):
        self.probabilities = probabilities or {}
        self.calls = 0

    def evaluate(self, version, concept, evaluator):
        self.calls += 1
        value = self.probabilities.get(
            (version["text"], concept["id"]), self.probabilities.get(version["text"])
        )
        if value is None:
            raise ValueError("No fixture outcome for this input")
        if isinstance(value, Exception):
            raise value
        return Evaluation(
            value,
            evaluator["model"],
            {"input_tokens": 0},
            [{"source_version": version["id"], "scope": "synthetic_fixture"}],
        )
