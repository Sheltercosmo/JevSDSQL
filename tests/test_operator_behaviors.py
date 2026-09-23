from itertools import product

import pytest
from sqlalchemy import select, update

from sdd.operators import schema
from sdd.operators.service import OperatorService, manifest
from sdd.operators.types import Decision, OutputState as Out
from sdd.operators.workflows import compose_tables
from test_operator_runtime import operators as operator_fixture

operators = operator_fixture


def content(result):
    return result.get("value") or result.get("partial_value")


@pytest.mark.parametrize(
    "operator,arguments",
    [
        ("PROMPT", {"subjects": ["a"], "instructions": "p", "output_type": "noul"}),
        ("CHOICE", {"state": "a", "question": "p", "options": {"a": "First", "b": "Second"}}),
        ("SCORE", {"state": "a", "rubric_levels": ["Absent", "Present"]}),
        ("CLASSIFY", {"subjects": ["a"], "taxonomy_rev": {"options": {"a": "First"}}}),
        ("FILTER", {"relation": ["a"], "concept_rev": "p"}),
        ("RANK", {"subjects": ["a", "b"], "criterion": "p", "top_k": 1}),
        ("COMPARE", {"left": "a", "right": "b", "criterion": "p"}),
        ("RERANK", {"query": "q", "candidates": ["a"], "criterion": "p"}),
        ("EXTRACT", {"subjects": ["First. Second."], "field_spec": "p"}),
        ("FIND", {"corpus": ["First. Second."], "query": "p"}),
        (
            "STRUCTURE",
            {"lines": ["First\n", "Second\n"], "block_taxonomy": {"options": {"p": "Paragraph"}}},
        ),
        ("SUMMARY_EXTRACTIVE", {"subjects": ["First. Second."], "facet": "p"}),
        (
            "ROUTE",
            {
                "request": "q",
                "allowed_handlers": {"lookup": "Read records"},
                "candidate_args": {"scope": {"all": "All records", "recent": "Recent records"}},
            },
        ),
        ("JOIN", {"left": ["a"], "right": ["b"], "predicate": "p"}),
        (
            "ALIGN",
            {
                "left": [{"name": "A"}],
                "right": [{"name": "B"}],
                "candidate_policy": {"kind": "all_pairs"},
                "identity_definition": "Same entity",
            },
        ),
        ("VERIFY", {"claim": "q", "evidence": ["a"]}),
        ("RELATE", {"left": ["a"], "right": ["b"], "relation_rev": "p"}),
        ("COVER", {"obligations": ["q"], "evidence_scope": ["a"]}),
        (
            "CONTRAST",
            {"left": [{"unit": 1}], "right": [{"unit": 2}], "concepts": ["p"], "unit": "unit"},
        ),
        (
            "MATCH",
            {
                "records": [{"id": "a", "time": 1}, {"id": "b", "time": 2}],
                "typed_pattern": {
                    "nodes": [
                        {"id": "first", "instructions": "p"},
                        {"id": "second", "instructions": "q"},
                    ],
                    "edges": [{"left": "first", "right": "second", "before": "time"}],
                },
            },
        ),
        ("EVIDENCE_JOIN", {"claims": ["q"], "candidate_sources": ["a", "b"]}),
        ("ENSURE_SEMANTICS", {"subject_revs": ["a"], "concept_revs": ["p"]}),
        (
            "EXPLAIN_PLAN",
            {"typed_plan": {"operator": "TAG"}, "source_stats": {"subjects": 100, "questions": 2}},
        ),
        (
            "CLASSIFY_HIERARCHY",
            {
                "subjects": ["a"],
                "taxonomy_rev": {
                    "root": "root",
                    "nodes": {"root": {"children": ["leaf"]}, "leaf": {"description": "Category"}},
                },
            },
        ),
    ],
)
def test_operator_contracts_are_executable(operators, operator, arguments):
    _, _, service = operators
    result = service.call(operator, arguments)
    assert result["output_state"] == "VALUE", result
    assert result["manifest"]["answer_complete"]
    assert content(result) is not None


def test_manifest_has_exactly_forty_one_implemented_functions(operators):
    _, _, service = operators
    functions = manifest()["functions"]
    assert len(functions) == 41
    assert all(callable(getattr(service, f["name"].split(".")[1].lower())) for f in functions)


def test_source_spans_dates_and_ties(operators):
    _, _, service = operators
    result = service.call(
        "EXTRACT", {"subjects": ["完成。承诺明天交付。"], "field_spec": "已经完成的陈述"}
    )
    span = content(result)["0"][0]
    assert span["text"] == "完成。承诺明天交付。"[span["start"] : span["end"]]
    dates = service.call(
        "EXTRACT_DATE",
        {
            "subjects": ["昨天是2026年9月21日"],
            "reference_time": "2026-09-22T10:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "locale": "zh-CN",
        },
    )
    assert {v["date"]["value"] for v in content(dates)["0"]} == {"2026-09-21"}
    ambiguous = service.call(
        "EXTRACT_DATE", {"subjects": ["01/02/2026"], "timezone": "Asia/Shanghai", "locale": "zh-CN"}
    )
    assert ambiguous["output_state"] == "UNKNOWN"
    with pytest.raises(ValueError, match="UTC offset"):
        service.call(
            "EXTRACT_DATE",
            {
                "subjects": ["today"],
                "reference_time": "2026-09-22",
                "timezone": "UTC",
                "locale": "en-GB",
            },
        )
    ranked = service.call("RANK", {"subjects": ["b", "a"], "criterion": "yes", "top_k": 1})
    assert len(content(ranked)["ranked"]) == 2


def test_composite_reweighting_uses_same_raw_scores(operators):
    _, model, service = operators
    args = {
        "subjects": ["a"],
        "rubrics": {
            "x": {"instructions": "x", "criteria": ["Low", "High"]},
            "y": {"instructions": "y", "criteria": ["Low", "Medium", "High"]},
        },
        "weights": {"x": 1, "y": 1},
    }
    first = service.call("COMPOSITE_SCORE", args)
    second = service.call("COMPOSITE_SCORE", {**args, "weights": {"x": 3, "y": 1}})
    assert model.calls == 1
    assert content(first)["0"]["composite"]["value"] == 0.75
    assert content(second)["0"]["composite"]["value"] == 0.875


def test_absence_and_population_bounds_do_not_treat_unknown_as_false(operators):
    _, _, service = operators
    args = {"left": ["a"], "right": ["b"], "predicate": "ambiguous", "join_type": "anti"}
    unknown = service.call("JOIN", args)
    assert content(unknown)["rows"] == [] and content(unknown)["unmatched_left"] == []
    absent = service.call("JOIN", {**args, "predicate": "no"})
    assert content(absent)["unmatched_left"] == ["0"]
    pruned = service.call(
        "JOIN", {**args, "predicate": "no", "candidate_policy": {"kind": "explicit", "pairs": []}}
    )
    assert content(pruned)["unmatched_left"] == [] and pruned["output_state"] == "UNKNOWN"
    aggregate = service.call(
        "AGGREGATE",
        {
            "relation": [{"group": "g", "n": 1}, {"group": "g", "n": 2}],
            "semantic_predicates": ["ambiguous"],
            "group_by": ["group"],
        },
    )
    assert content(aggregate)["groups"][0]["count_bounds"] == [0, 2]
    assert content(aggregate)["groups"][0]["value"] is None
    total = service.call(
        "AGGREGATE",
        {
            "relation": [{"n": 1}, {"n": 2}],
            "semantic_predicates": ["yes"],
            "metric": {"kind": "sum", "field": "n"},
        },
    )
    assert content(total)["groups"][0]["value"] == 3


def test_evidence_conflict_and_isolated_bundles(operators):
    _, model, service = operators
    verified = service.call("VERIFY", {"claim": "q", "evidence": ["A", "B"]})
    assert content(verified)["stance"]["value"] == "conflict"
    model.payloads.clear()
    result = service.call("EVIDENCE_JOIN", {"claims": ["q"], "candidate_sources": ["A", "B", "C"]})
    assert content(result)["tested_subset_space"] == 6
    assert all(len(state["evidence"]) in {1, 2} for _, state, _ in model.payloads)
    assert (
        service.stance(Decision.unexecuted("budget"), Decision.known(False)).output_state
        == Out.NOT_EVALUATED
    )


def test_resolve_retains_skips_after_answer_settles(operators):
    _, model, service = operators
    result = service.call(
        "RESOLVE",
        {
            "plan": {"subjects": ["a", "b", "c"], "predicate": "yes"},
            "objective": {"kind": "exists"},
            "wave_size": 1,
        },
    )
    assert result["output_state"] == "VALUE" and content(result)["answer"]["value"] is True
    assert result["manifest"]["not_evaluated"] == 2 and model.calls == 1
    assert all(
        d["value"] is None
        for d in result["observations"].values()
        if d["output_state"] == "NOT_EVALUATED"
    )
    exact = service.call(
        "RESOLVE",
        {
            "plan": {"subjects": ["a", "b", "c"], "predicate": "yes"},
            "objective": {"kind": "exact_count"},
            "wave_size": 1,
        },
        limits={"max_stages": 1},
    )
    assert exact["output_state"] == "UNKNOWN" and content(exact)["bounds"] == [1, 3]


def test_speculative_state_scan_and_associative_set_composition(operators):
    _, model, service = operators
    machine = {
        "states": {"open": "Open", "done": "Completed"},
        "sufficient_history": "Status summarizes all necessary history",
        "instructions": "Only explicit completion changes status",
    }
    result = service.call(
        "STATE_SCAN",
        {
            "blocks": ["Promise", "Completion"],
            "state_machine_rev": machine,
            "initial_state": "open",
        },
    )
    assert result["manifest"]["eligible"] == 4 and model.peak > 1
    assert not content(result)["hypotheses_are_unconditional_labels"]
    a = {"open": ["open", "done"], "done": ["done"]}
    b = {"open": ["done"], "done": ["open"]}
    c = {"open": ["open"], "done": ["done"]}
    assert compose_tables(compose_tables(a, b), c) == compose_tables(a, compose_tables(b, c))
    for x, y, z in product((a, b, c), repeat=3):
        assert compose_tables(compose_tables(x, y), z) == compose_tables(x, compose_tables(y, z))


def test_workflow_validation_is_before_inference_and_guards_are_typed(operators):
    _, model, service = operators
    with pytest.raises(ValueError, match="cycle"):
        service.call(
            "WORKFLOW",
            {
                "stages": [
                    {"id": "root", "literal": True},
                    {"id": "a", "literal": True, "depends_on": ["b"]},
                    {"id": "b", "literal": True, "depends_on": ["a"]},
                ]
            },
        )
    assert model.calls == 0
    result = service.call(
        "WORKFLOW",
        {
            "stages": [
                {"id": "root", "literal": 1},
                {"id": "a", "literal": True, "when": {"stage": "root", "equals": True}},
            ]
        },
    )
    assert content(result)["stages"]["a"]["operation_state"] == "BLOCKED_BY_POLICY"
    skipped = service.call(
        "WORKFLOW",
        {
            "stages": [
                {"id": "root", "literal": False},
                {"id": "a", "literal": True, "when": {"stage": "root", "equals": True}},
            ]
        },
    )
    assert (
        skipped["output_state"] == "VALUE"
        and content(skipped)["stages"]["a"]["output_state"] == "NOT_EVALUATED"
    )


def test_trace_chronology_and_completed_event_contract(operators):
    _, _, service = operators
    result = service.call(
        "TRACE",
        {
            "records": [{"entity": "x", "time": "2026-01-02T00:00:00Z", "text": "Completed"}],
            "entity_scope": "entity",
            "event_time": "time",
            "process_rev": {
                "events": {"completed": "Explicit completed action"},
                "transitions": {"open": {"completed": "done"}, "done": {}},
                "initial_state": "open",
            },
        },
    )
    assert content(result)["traces"][0]["current_state"] == "done"


def test_governance_review_materialization_and_refresh(operators):
    db, model, service = operators
    proposal = service.call(
        "DISCOVER",
        {
            "records": ["A failed payment"],
            "catalog_rev": None,
            "facet": "issue",
            "discovery_budget": {"holdout": ["Card rejected"]},
        },
    )
    candidate = content(proposal)["candidates"][0]["candidate_revision"]
    with pytest.raises(ValueError, match="holdout"):
        service.call(
            "PROMOTE",
            {
                "candidate_rev": candidate,
                "owner": "owner",
                "validation_report": {},
                "retention_policy": "30 days",
            },
        )
    promoted = service.call(
        "PROMOTE",
        {
            "candidate_rev": candidate,
            "owner": "owner",
            "validation_report": {
                "independent_holdout": ["human-reviewed-holdout"],
                "reviewed_examples": [{"text": "Card rejected", "expected": True}],
            },
            "retention_policy": "30 days",
        },
    )
    revision = content(promoted)["approved_revision"]["id"]
    dataset = service.catalog.create(
        service.tenant, "Notes", [{"id": 1, "text": "Card rejected"}], primary_key=["id"]
    )
    args = {
        "concept_rev": revision,
        "target_scope": {"dataset_id": dataset["id"]},
        "refresh_policy": {"mode": "explicit"},
    }
    partial = service.call("MATERIALIZE", args, limits={"max_judgments": 0})
    assert not content(partial)["published"]
    result = service.call("MATERIALIZE", args)
    generation = content(result)["generation"]["id"]
    assert content(result)["published"]
    observation = next(iter(result["observations"].values()))["observation_id"]
    review = service.call(
        "REVIEW",
        {
            "observations": [
                {"observation_id": observation, "value": False, "reason": "Human correction"}
            ]
        },
    )
    assert content(review)["raw_observations_unchanged"]
    assert service.store.get(schema.observations, observation)["answer"]["noul"] == 0.95
    refreshed = service.call(
        "REFRESH", {"change_set": {"source_revisions": [dataset["id"]]}, "policies": [generation]}
    )
    assert content(refreshed)["replacement_generations"][0]["published"]
    reader = OperatorService(db, model, service.tenant, "reader")
    with pytest.raises(PermissionError):
        reader.call("REVIEW", {"observations": []})


def test_source_changed_during_provider_call_cannot_publish_or_cache(operators):
    db, model, service = operators
    dataset = service.catalog.create(
        service.tenant, "Notes", [{"id": 1, "text": "Before"}], primary_key=["id"]
    )
    original = model.ask

    def mutate(*args):
        answer = original(*args)
        with db.transaction(service.tenant) as connection:
            table = service.catalog.table(dataset, connection)
            connection.execute(update(table).values(text="After"))
        return answer

    model.ask = mutate
    result = service.call("TAG", {"subjects": {"dataset_id": dataset["id"]}, "concept_revs": ["p"]})
    assert result["operation_state"] == "STALE" and result["manifest"]["decided"] == 0
    with db.transaction(service.tenant) as connection:
        assert connection.execute(select(schema.observations)).all() == []


def test_schema_fields_and_held_sql_proposal(operators, monkeypatch):
    _, model, service = operators
    service.catalog.create(service.tenant, "Notes", [{"id": 1, "text": "Done"}], primary_key=["id"])
    selection = service.call("SELECT_SCHEMA", {"request": "Find completed notes"})
    assert len(content(selection)["tables"][0]["columns"]) == 2 and model.calls == 1

    class Planner:
        def __init__(self, db, decisions, strategy):
            self.decisions = decisions

        def plan(self, tenant, question, dataset_ids):
            self.decisions.ask(tenant, question, {"p": {"type": "noul", "instructions": "p"}})
            return {"status": "hold", "sql": "SELECT 1", "reason": "Requires review"}

    monkeypatch.setattr("sdd.operators.service.Planner", Planner)
    result = service.call("PLAN_SQL", {"request": "q"})
    assert content(result)["plan"]["status"] == "hold" and not content(result)["executed"]


def test_explain_plan_does_not_assume_different_states_share_a_request(operators):
    _, _, service = operators
    result = service.call(
        "EXPLAIN_PLAN",
        {"typed_plan": {"operator": "TAG"}, "source_stats": {"subjects": 100, "questions": 2}},
    )
    assert content(result)["requests"] == 200
    warm = service.call(
        "EXPLAIN_PLAN",
        {
            "typed_plan": {"operator": "TAG"},
            "source_stats": {"subjects": 100, "questions": 2},
            "cache_stats": {"compatible_judgments": 200},
        },
    )
    assert content(warm)["requests"] == content(warm)["input_token_upper_bound"] == 0


def test_sql_aggregate_preserves_decimals_large_integers_and_empty_count(operators):
    _, _, service = operators
    dataset = service.catalog.create(
        service.tenant,
        "Balances",
        [{"id": 1, "amount": "9007199254740993.01"}, {"id": 2, "amount": "0.02"}],
        columns=[{"name": "id", "type": "integer"}, {"name": "amount", "type": "number"}],
        primary_key=["id"],
    )
    result = service.call(
        "AGGREGATE",
        {
            "relation": {"dataset_id": dataset["id"]},
            "semantic_predicates": [],
            "metric": {"kind": "sum", "field": "amount"},
        },
    )
    # SQLite's source NUMERIC affinity may already round huge decimals; explicit decimal input remains exact.
    exact = service.call(
        "AGGREGATE",
        {
            "relation": [{"amount": "9007199254740993.01"}, {"amount": "0.02"}],
            "semantic_predicates": [],
            "metric": {"kind": "sum", "field": "amount", "value_type": "decimal"},
        },
    )
    assert content(exact)["groups"][0]["value"] == "9007199254740993.03"
    assert result["output_state"] == "VALUE"
    large = service.call(
        "AGGREGATE",
        {
            "relation": [{"n": 9007199254740993}, {"n": 2}],
            "semantic_predicates": [],
            "metric": {"kind": "sum", "field": "n"},
        },
    )
    assert content(large)["groups"][0]["value"] == 9007199254740995
    empty = service.call("AGGREGATE", {"relation": [], "semantic_predicates": []})
    assert content(empty)["groups"][0]["value"] == 0
