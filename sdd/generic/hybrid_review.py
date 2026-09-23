"""One parallel wave for SQL obligations, coverage and candidate choice."""

from copy import deepcopy
import time

from sqlglot import parse_one, exp

from .hybrid_contract import sql_facts
from .hybrid_feedback import review_context, output_review_jobs
from .jev import noul, choice, selected


CHECKS = {
    "outputs": "Return exactly the requested answer fields and grain. Check missing fields, duplicates, tie handling and limits; supporting measures are not automatically answer fields.",
    "population": "Use the requested eligible population and Boolean/time boundaries. Distinguish NULL, blank strings and zero. If a record needs several measurements, check required completeness rather than assuming any one value suffices.",
    "joins": "Follow intended relationship roles and avoid multiplying independent one-to-many measures. Preserve required entities and denominators.",
    "stages": "Apply latest-record selection, aggregation, windows and filtering in their required dependency order and partition scope.",
    "formulas": "Apply stated business formulas, units, numeric types, constants and denominators correctly.",
    "coverage": "Cover the original objective and supplied rules. Detect missing operands or an unjustified choice between plausible source meanings.",
    "overall": "This complete SQL must satisfy the original objective. Inspect its composition and unsupported assumptions, not its label or explanation.",
}
ISSUES = {
    "none": "No concrete error established",
    "output": "Specific requested output is missing or a specific output is unrequested",
    "population": "Specific required predicate, time restriction or missing-value rule is wrong",
    "relation": "Specific join role or aggregation population is wrong",
    "calculation": "Specific formula, unit conversion or arithmetic operation is wrong",
    "coverage": "A specific requested operation or required operand is missing",
    "ambiguous": "Several interpretations are plausible; no concrete correction is justified",
}


def review_candidates(tenant, packet, filtered, candidates, decisions):
    start = time.perf_counter()
    jobs, operation_keys = [], {}
    present = {(t["name"], c["name"]) for t in filtered["catalog"] for c in t["columns"]}
    rules = {str(r["id"]) for r in filtered["business_knowledge"]}
    omitted = [
        {"kind": "column", "detail": {"table": t["name"], **c}}
        for t in packet["catalog"]
        for c in t["columns"]
        if (t["name"], c["name"]) not in present
    ] + [
        {"kind": "rule", "detail": r}
        for r in packet["business_knowledge"]
        if str(r["id"]) not in rules
    ]
    for start_index in range(0, len(omitted), 32):
        page = omitted[start_index : start_index + 32]
        jobs.append(
            (
                {
                    "request": packet["request"],
                    "candidate_sql": {c["id"]: c["sql"] for c in candidates},
                    "additional_data": {
                        "d" + str(start_index + i): item for i, item in enumerate(page)
                    },
                },
                {
                    "check_additional_d" + str(start_index + i): noul(
                        "Would item d"
                        + str(start_index + i)
                        + " materially help resolve a specific missing operand or interpretation in this SQL? Do not assume an unused field is required. The original objective is authoritative."
                    )
                    for i in range(len(page))
                },
            )
        )
    compact = review_context(filtered)
    facts_by_id = {}
    for candidate in candidates:
        identity = candidate["id"]
        facts = sql_facts(candidate["sql"], packet)
        facts_by_id[identity] = facts
        names = {d.source.casefold() for d in facts["data_constraints"]}
        relevant = [t for t in compact["catalog"] if t["name"].casefold() in names]
        state = {
            "request": packet["request"],
            "catalog": relevant,
            "business_knowledge": packet["business_knowledge"],
            "candidate_id": identity,
            "candidate_sql": candidate["sql"],
            "implemented_operations": [s.model_dump() for s in facts["steps"]],
            "fact_source": "sql_ast; implemented behavior, not verified intent",
        }
        questions = {
            "check_" + identity + "_" + key: noul(
                instruction
                + " Judge actual SQL against the ORIGINAL request. Uncertainty is not a concrete error."
            )
            for key, instruction in CHECKS.items()
        }
        operations = [
            s for s in facts["steps"] if s.operator in ("filter", "aggregate", "window", "join")
        ]
        operation_keys[identity] = []
        for i, operation in enumerate(operations):
            key = f"check_{identity}_operation{i}"
            operation_keys[identity].append(key)
            questions[key] = noul(
                f"Is implemented step {operation.name} necessary for the original objective? Check its expressions and dependencies. Necessary intermediate operations count; unsupported restrictions and repeated aggregation do not."
            )
        tree = parse_one(candidate["sql"], read="postgres")
        joins = [
            j
            for j in tree.find_all(exp.Join)
            if not j.side and j.kind in ("", "INNER") and j.args.get("on")
        ]
        if joins:
            state["inner_joins"] = {f"j{i}": j.sql(dialect="postgres") for i, j in enumerate(joins)}
            questions.update(
                {
                    f"check_preserve_{identity}_j{i}": noul(
                        f"Must join j{i} preserve every left input record when no right record exists, according to the original requested population? Return low when the join establishes eligibility. Check downstream predicates too."
                    )
                    for i in range(len(joins))
                }
            )
        questions["check_" + identity + "_issue"] = choice(
            "Identify the concrete defect, if any. Select ambiguous for unresolved meanings and none for a merely low confidence. A correction must be supported by the request and actual SQL, not a guess.",
            ISSUES,
        )
        jobs.append((state, questions))
    jobs.extend(output_review_jobs(packet, candidates, noul, choice))
    if len(candidates) > 1:
        jobs.append(
            (
                {
                    "request": packet["request"],
                    "catalog": compact["catalog"],
                    "business_knowledge": packet["business_knowledge"],
                    "candidates": [{k: c[k] for k in ("id", "sql")} for c in candidates],
                    "option_sql": {"hybrid_candidate": {c["id"]: c["sql"] for c in candidates}},
                },
                {
                    "hybrid_candidate": choice(
                        "Choose the complete SQL best satisfying the original request. Prefer fewer unsupported assumptions. This independent choice will be reconciled with SQL checks; never combine fragments.",
                        {c["id"]: "Complete candidate " + c["id"] for c in candidates},
                    )
                },
            )
        )
    results = decisions.ask_many(tenant, jobs, allow_partial=True)
    answers = {k: v for r in results for k, v in r["answers"].items()}
    failures = [r["failure"] for r in results if r.get("failure")]
    checks = {k: v["noul"] for k, v in answers.items() if v["type"] == "noul"}
    ranked = sorted(candidates, key=lambda c: quality(c["id"], checks), reverse=True)
    winner = ranked[0]
    if "hybrid_candidate" in answers:
        answer = answers["hybrid_candidate"]
        proposed = next(c for c in candidates if c["id"] == selected(answer))
        if (
            answer.get("_selection")
            or quality(proposed["id"], checks)[0] >= quality(winner["id"], checks)[0] - 0.1
        ):
            winner = proposed
    overall = checks.get("check_" + winner["id"] + "_overall")
    additions = [
        item
        for i, item in enumerate(omitted)
        if checks.get("check_additional_d" + str(i), 0) >= 0.8
    ]
    expanded = deepcopy(filtered)
    for item in additions:
        if item["kind"] == "rule":
            expanded["business_knowledge"].append(item["detail"])
            continue
        detail = item["detail"]
        table = next((t for t in expanded["catalog"] if t["name"] == detail["table"]), None)
        if table is None:
            original = next(t for t in packet["catalog"] if t["name"] == detail["table"])
            table = {**original, "columns": []}
            expanded["catalog"].append(table)
        table["columns"].append({k: v for k, v in detail.items() if k != "table"})
    inspection = {
        "order": [
            "coverage, operations, candidate checks and choice in parallel",
            "local reconciliation",
        ],
        "additional_data": {
            "output_state": "UNKNOWN" if failures else "VALUE",
            "operation_state": "FAILED" if failures else "COMPLETED",
            "helpful": additions,
        },
        "additional_rows": {
            "output_state": "NOT_EVALUATED",
            "operation_state": "SKIPPED",
            "reason": "Reuse bounded value evidence already supplied before generation",
        },
        "operation_necessity": {
            "output_state": "UNKNOWN"
            if failures
            or any(
                0.2 < checks[k] < 0.8
                for keys in operation_keys.values()
                for k in keys
                if k in checks
            )
            else "VALUE",
            "operation_state": "FAILED" if failures else "COMPLETED",
            "probabilities": {
                k: checks[k] for keys in operation_keys.values() for k in keys if k in checks
            },
        },
    }
    issue = answers.get("check_" + winner["id"] + "_issue")
    tickets = []
    if issue:
        kind = selected(issue)
        support = issue["probabilities"][kind]
        dimension = {"output": "outputs", "relation": "joins", "calculation": "formulas"}.get(
            kind, kind
        )
        if (
            kind not in ("none", "ambiguous")
            and support >= 0.8
            and checks.get("check_" + winner["id"] + "_" + dimension, 1) <= 0.2
        ):
            tickets.append(
                {
                    "kind": kind,
                    "candidate": winner["id"],
                    "support": support,
                    "output_state": "VALUE",
                    "sql": winner["sql"],
                    "evidence": {
                        k: v
                        for k, v in checks.items()
                        if k.startswith("check_" + winner["id"] + "_") and v <= 0.2
                    },
                    "problem": ISSUES[kind],
                }
            )
    return {
        "selected": winner["id"],
        "checks": checks,
        "repair_tickets": tickets,
        "output_roles": {k: v for k, v in answers.items() if k.startswith("check_role_")},
        "plan_inspection": inspection,
        "overall_appropriateness": {
            "probability": overall,
            "output_state": "NOT_EVALUATED"
            if overall is None
            else "UNKNOWN"
            if 0.2 < overall < 0.8
            else "VALUE",
            "operation_state": "FAILED" if overall is None else "COMPLETED",
        },
        "review_state": {
            "output": "NOT_EVALUATED" if not answers else "UNKNOWN" if failures else "VALUE",
            "operation": "FAILED" if failures else "COMPLETED",
            **({"code": failures[0]["code"]} if failures else {}),
        },
        "review_wave": {
            "jobs": len(jobs),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            "failures": failures,
            "placement": "All checks read the same immutable candidate SQL; selection waits for their completion.",
        },
        "_expanded": expanded,
        "_uncertain": bool(failures)
        or quality(winner["id"], checks)[0] < 0.8
        or facts_by_id[winner["id"]]["output_state"] != "VALUE",
        "_facts": facts_by_id,
    }


def quality(identity, checks):
    scores = [v for k, v in checks.items() if k.startswith("check_" + identity + "_")]
    return min(scores, default=0), sum(scores)
