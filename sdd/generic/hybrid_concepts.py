"""Optional small intent expansion; its hypotheses never become row predicates."""

import os

from pydantic import BaseModel, ConfigDict, Field

from ..evaluators import ProviderError
from ..ledger import digest
from .catalog import serial
from .jev import noul


class Concepts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    related_concepts: list[str] = Field(max_length=12)
    row_criteria: list[str] = Field(max_length=8)
    ambiguities: list[str] = Field(max_length=8)


def expand_concepts(tenant, packet, decisions, llm, previous=None):
    mode = os.getenv("SDD_HYBRID_CONCEPTS", "off")
    if mode not in {"off", "auto", "on"}:
        raise ValueError("SDD_HYBRID_CONCEPTS must be off, auto or on")
    signature = digest([packet["request"], mode])
    cached = (previous or {}).get("_hybrid_concepts")
    if cached and cached["signature"] == signature:
        return {
            **cached,
            "generation": {**cached.get("generation", {}), "calls": 0, "reused": True},
        }
    needed = mode == "on"
    if mode == "auto":
        try:
            answer = decisions.ask(
                tenant,
                {
                    "request": packet["request"],
                    "table_names": [t["name"] for t in packet["catalog"]],
                    "rule_names": [
                        r.get("name", r.get("knowledge", "")) for r in packet["business_knowledge"]
                    ],
                },
                {
                    "check_concept_expansion": noul(
                        "Does this request need concept expansion before schema retrieval? Say yes for vague business "
                        "objectives, implicit semantic categories or vocabulary requiring synonyms. Say no for explicit "
                        "metric/rule names, concrete field operations or straightforward requests. Complexity alone is not a reason."
                    )
                },
            )["answers"]["check_concept_expansion"]
            needed = answer["noul"] >= 0.8
        except ProviderError:
            return {
                "signature": signature,
                "output_state": "NOT_EVALUATED",
                "operation_state": "FAILED",
                "generation": {"calls": 0},
            }
    if not needed:
        return {
            "signature": signature,
            "output_state": "NOT_EVALUATED",
            "operation_state": "SKIPPED",
            "generation": {"calls": 0},
        }
    try:
        value, generation = llm.generate(
            "Suggest possible concepts for this database question. This is a short retrieval aid, not a SQL plan. "
            "Include likely synonyms (including English/Simplified Chinese when useful), entity/metric roles, "
            "and possible row-selection meanings. Keep each phrase short. Preserve ambiguity; do not invent "
            "schema names, numeric thresholds, business definitions or facts. Suggestions are hypotheses only. "
            "No tools. Request: " + packet["request"],
            Concepts.model_json_schema(),
        )
        concepts = Concepts.model_validate(value).model_dump()
        return {
            "signature": signature,
            "output_state": "VALUE",
            "operation_state": "COMPLETED",
            "hypotheses": concepts,
            "generation": generation,
        }
    except (ProviderError, ValueError) as exc:
        return {
            "signature": signature,
            "output_state": "NOT_EVALUATED",
            "operation_state": "FAILED",
            "code": getattr(exc, "code", "InvalidConcepts"),
            "generation": {"calls": 1},
        }


def retrieve_row_evidence(tenant, packet, hypotheses, catalog, decisions):
    """Filter a bounded value sample for context, never the SQL result population."""
    criteria = hypotheses.get("row_criteria", [])
    if not criteria:
        return packet, {"output_state": "NOT_EVALUATED", "operation_state": "SKIPPED"}
    fields = [
        (t["name"], c["name"])
        for t in packet["catalog"]
        for c in t["columns"]
        if c["type"] == "text" and c["name"] not in t["primary_key"]
    ][:12]
    values = []
    try:
        for item in packet["catalog"]:
            for column in item["columns"]:
                if (item["name"], column["name"]) not in fields:
                    continue
                for value in column.get("value_evidence", {}).get("examples", [])[:8]:
                    values.append(
                        {"table": item["name"], "column": column["name"], "value": str(value)[:160]}
                    )
        jobs = []
        for start in range(0, len(values), 32):
            page = values[start : start + 32]
            jobs.append(
                (
                    {
                        "request": packet["request"],
                        "hypotheses": hypotheses,
                        "sampled_values": {"v" + str(start + i): v for i, v in enumerate(page)},
                    },
                    {
                        "check_sample_v" + str(start + i): noul(
                            "Could sampled value v"
                            + str(start + i)
                            + " help interpret a requested row criterion? "
                            "Treat concept suggestions as hypotheses. This decides context relevance, not row eligibility."
                        )
                        for i, _ in enumerate(page)
                    },
                )
            )
        answers = {
            k: v["noul"] for r in decisions.ask_many(tenant, jobs) for k, v in r["answers"].items()
        }
        retained = [v for i, v in enumerate(values) if answers["check_sample_v" + str(i)] >= 0.2]
        evidence = {
            "sample_only": True,
            "complete": False,
            "values": serial(retained),
            "rule": "Observed examples only. Omission is not evidence of absence; do not change aggregation/window populations from this sample.",
        }
        return {**packet, "row_evidence": evidence}, {
            "output_state": "VALUE",
            "operation_state": "TRUNCATED",
            "sampled": len(values),
            "retained": len(retained),
            "changes_query_population": False,
        }
    except Exception as exc:
        return packet, {
            "output_state": "NOT_EVALUATED",
            "operation_state": "FAILED",
            "code": type(exc).__name__,
            "changes_query_population": False,
        }
