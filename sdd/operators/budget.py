"""Conservative payload admission and reservations shared by all stages of a job."""

import json
import math
import os
from dataclasses import asdict, dataclass
from threading import Lock


def token_bound(payload):
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 32


@dataclass(frozen=True)
class Limits:
    max_judgments: int = 1000
    max_requests: int = 1000
    max_input_tokens: int = 2_000_000
    max_input_usd: float = 2.0
    concurrency: int = 4
    batch_size: int = 32
    retries: int = 1
    max_stages: int = 3

    def __post_init__(self):
        for key in (
            "max_judgments",
            "max_requests",
            "max_input_tokens",
            "concurrency",
            "batch_size",
            "retries",
            "max_stages",
        ):
            value = getattr(self, key)
            if type(value) is not int or value < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        if not 1 <= self.concurrency <= 16 or not 1 <= self.batch_size <= 32:
            raise ValueError("Concurrency is 1–16 and question bundles are 1–32")
        if self.retries > 3 or not 1 <= self.max_stages <= 6:
            raise ValueError("At most three retries and six bounded stages")
        if not math.isfinite(self.max_input_usd) or self.max_input_usd < 0:
            raise ValueError("Input cost cap must be finite and nonnegative")


class Budget:
    def __init__(self, limits=None):
        self.limits = limits or Limits()
        self.lock = Lock()
        self.judgments = self.requests = self.tokens = 0
        self.actual_input = self.actual_output = 0
        self.price = float(os.getenv("SDD_JEV_INPUT_USD_PER_MILLION", "0.042"))
        self.hard_work = int(os.getenv("SDD_OPERATOR_HARD_JUDGMENTS", "1000000"))
        self.hard_requests = int(os.getenv("SDD_OPERATOR_HARD_REQUESTS", "100000"))
        self.hard_usd = float(os.getenv("SDD_OPERATOR_HARD_USD", "20"))
        if (
            not math.isfinite(self.price)
            or self.price < 0
            or not math.isfinite(self.hard_usd)
            or self.hard_usd <= 0
            or min(self.hard_work, self.hard_requests) <= 0
        ):
            raise ValueError("Invalid server cost or hard-budget policy")

    def reserve(self, judgments, tokens):
        with self.lock:
            work, requests, total = (
                self.judgments + judgments,
                self.requests + 1,
                self.tokens + tokens,
            )
            if (
                work > self.limits.max_judgments
                or work >= self.hard_work
                or requests > self.limits.max_requests
                or requests >= self.hard_requests
                or total > self.limits.max_input_tokens
                or total * self.price / 1e6 > self.limits.max_input_usd
                or total * self.price / 1e6 >= self.hard_usd
            ):
                return False
            self.judgments, self.requests, self.tokens = work, requests, total
            return True

    def usage(self, usage):
        with self.lock:
            self.actual_input += usage.get("input_tokens", 0)
            self.actual_output += usage.get("output_tokens", 0)

    def manifest(self):
        return {
            "limits": asdict(self.limits),
            "reserved_judgments": self.judgments,
            "reserved_requests": self.requests,
            "reserved_input_tokens": self.tokens,
            "actual_input_tokens": self.actual_input,
            "actual_output_tokens": self.actual_output,
            "input_usd": self.actual_input * self.price / 1e6,
            "token_accounting": "Conservative UTF-8 byte bound plus envelope overhead; not the provider tokenizer or an ETA",
        }


def expansion(operator, left, right=0, questions=1, bundle_size=2, states=1):
    if any(type(n) is not int or n < 0 for n in (left, right, questions, bundle_size, states)):
        raise ValueError("Work cardinalities must be nonnegative integers")
    if operator in {"JOIN", "ALIGN", "RELATE", "COVER"}:
        return left * right * questions
    if operator == "EVIDENCE_JOIN":
        return (
            left
            * sum(math.comb(right, i) for i in range(1, min(bundle_size, right) + 1))
            * questions
        )
    if operator == "STATE_SCAN":
        return left * states
    if operator == "PAIRWISE_RANK":
        return left * max(0, left - 1) // 2
    return left * questions


def estimate(judgments, requests, tokens, limits=None, stages=1):
    budget = Budget(limits)
    if any(type(v) is not int or v < 0 for v in (judgments, requests, tokens, stages)):
        raise ValueError("Estimate inputs must be nonnegative integers")
    cost = tokens * budget.price / 1e6
    concurrency = budget.limits.concurrency
    floor = max(
        requests / (1200 * 0.7 / 60), tokens / (250000 * 0.7), math.ceil(requests / concurrency) * 2
    )
    warnings = []
    if judgments >= 10000 or requests >= 1000 or cost >= 2 or floor >= 30:
        warnings.append("W02")
    if judgments >= budget.hard_work or requests >= budget.hard_requests or cost >= budget.hard_usd:
        warnings.append("W03")
    return {
        "judgments": judgments,
        "requests": requests,
        "input_token_upper_bound": tokens,
        "input_usd_upper_bound": cost,
        "scheduling_floor_seconds": floor,
        "stages": stages,
        "warnings": warnings,
        "assumptions": "2s/request scenario, 70% of dated 1200 RPM / 250k TPS reference; not a completion promise",
    }
