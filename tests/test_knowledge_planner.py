import sqlite3

import pytest

from sdd.db import Database
from sdd.generic.catalog import Catalog
from sdd.generic.knowledge import RuleGraph, rules_from
from sdd.generic.planner import Planner, PlanReviewRequired
from sdd.generic.relational import Field
from sdd.generic.sql import SQLService
from sdd.generic.stage_dag import Column, operation as op, value


def graph(definitions):
    fields = {"x": Field("x", "readings", "amount", "number")}
    rules = rules_from(
        [
            {"id": str(i), "name": name, "definition": definition}
            for i, (name, definition) in enumerate(definitions)
        ]
    )
    return RuleGraph(rules, fields, {"x": Column("number", "amount")})


def evaluate(term, amount=5):
    with sqlite3.connect(":memory:") as connection:
        return connection.execute(
            "SELECT " + term.sql().sql(dialect="sqlite") + " FROM (SELECT ? AS x)", (amount,)
        ).fetchone()[0]


def test_formula_precedence_and_rounding_are_preserved():
    rule = graph([("Compound", "R = amount * (1 + amount / 10) * (1 - amount / 100)")])
    assert evaluate(rule.build("0")) == pytest.approx(7.125)
    assert evaluate(op("round", value(1.2345), value(2))) == 1.23
    assert evaluate(op("sub", value(10), op("sub", value(5), value(2)))) == 7


def test_shared_dependencies_are_references_not_expanded_trees():
    rule = graph([("Base", "B = amount + 1"), ("First", "F = B * 2"), ("Second", "S = B / 2")])
    rule.build("1")
    rule.build("2")
    assert len(rule.terms) == 3
    assert rule.terms["1"].columns() == {"k0"}
    assert rule.terms["2"].columns() == {"k0"}
    assert rule.layers == {"0": 1, "1": 2, "2": 2}


@pytest.mark.parametrize(
    "definition",
    [
        "R = __import__('os').system('anything')",
        "R = amount[0]",
        "R = [x for x in amount]",
        "R = open('file')",
        "R = amount; DELETE FROM readings",
        "R = missing + 1",
    ],
)
def test_unsupported_or_executable_expressions_remain_unknown(definition):
    rule = graph([("Unknown", definition), ("Unrequested", "U = amount")])
    with pytest.raises(ValueError):
        rule.build("0")
    evidence = rule.describe(["0"])
    assert evidence["unresolved"][0]["state"] == "UNKNOWN"
    assert "1" in evidence["not_evaluated"]
    assert not rule.terms


def test_cycles_do_not_become_false():
    rule = graph([("A", "A = B + 1"), ("B", "B = A + 1")])
    with pytest.raises(ValueError, match="Cyclic"):
        rule.build("0")


def test_zero_denominator_is_null():
    rule = graph([("Ratio", r"R = \frac{amount}{amount-5}")])
    assert evaluate(rule.build("0")) is None


def test_json_scalar_compiles_to_correct_sqlite_path():
    term = op("number", op("json_text", value('{"load": 7.5}'), value("load")))
    assert evaluate(term) == 7.5
    assert evaluate(op("json_text", value("{}"), value("missing"))) is None


class Model:
    model = "test"

    def ask(self, tenant, state, questions):
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": 0.99}
                continue
            options = question["criteria"]
            chosen = next(iter(options))
            if key.startswith("kb_field_"):
                field = state["fields"][key.removeprefix("kb_field_")]
                chosen = (
                    "output" if field["table"] == "accounts" and field["name"] == "id" else "unused"
                )
            elif key.startswith("kb_rule_"):
                chosen = "metric"
            elif key == "kb_count":
                chosen = "3"
            elif key == "kb_population":
                chosen = "accounts"
            elif key == "kb_join":
                chosen = "left"
            elif key.startswith("kb_output_"):
                index = int(key.rsplit("_", 1)[1])
                label = ["accounts.id", "Revenue", "Refunds"][index]
                chosen = next(k for k, v in options.items() if label in v)
            elif key.startswith("kb_agg_"):
                chosen = "value"
            elif key.startswith(("kb_sort_", "kb_round_")) or key == "kb_limit":
                chosen = "none"
            answers[key] = {
                "type": "choice",
                "choice": chosen,
                "probabilities": {k: float(k == chosen) for k in options},
            }
        return {"model": self.model, "answers": answers, "usage": {}}


def test_independent_child_aggregates_do_not_multiply_each_other(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "test.sqlite"))
    db.initialize()
    catalog = Catalog(db)
    catalog.create("a", "accounts", [{"id": 1}, {"id": 2}], primary_key=["id"])
    catalog.create(
        "a",
        "charges",
        [{"id": 1, "account": 1, "amount": 5}, {"id": 2, "account": 1, "amount": 7}],
        primary_key=["id"],
    )
    catalog.create(
        "a",
        "credits",
        [
            {"id": 1, "account": 1, "amount": 2},
            {"id": 2, "account": 1, "amount": 3},
            {"id": 3, "account": 1, "amount": 4},
        ],
        primary_key=["id"],
    )
    catalog.link("a", "charges", "accounts", "account", "id")
    catalog.link("a", "credits", "accounts", "account", "id")
    knowledge = [
        {
            "id": "0",
            "name": "Revenue",
            "definition": r"For a given account A, REV = \sum_{c \in \text{charges} \mid \text{account = A}} amount",
        },
        {
            "id": "1",
            "name": "Refunds",
            "definition": r"For a given account A, REF = \sum_{c \in \text{credits} \mid \text{account = A}} amount",
        },
    ]
    try:
        plan = Planner(db, Model(), "staged").plan(
            "a", "For every account show id, Revenue and Refunds", knowledge=knowledge
        )
    except PlanReviewRequired as exc:
        plan = exc.plan
    result = SQLService(db).execute("a", plan["logical_sql"])["result"]
    assert sorted([tuple(row.values()) for row in result]) == [(1, 12, 9), (2, None, None)]
    assert plan["stage_dag"]["max_independent_stages"] >= 2
    db.engine.dispose()


def test_aggregate_domain_predicates_are_never_silently_dropped():
    rule = graph(
        [
            (
                "Restricted",
                r"For a given record R, A = \sum_{i \in \text{readings} \mid \text{amount > 5}} amount",
            )
        ]
    )
    with pytest.raises(ValueError, match="restriction"):
        rule.build("0")


def test_unknown_boolean_is_not_counted_as_false():
    condition = op("eq", value(None), value(1))
    indicator = op(
        "case", condition, value(1), op("case", op("is_null", condition), value(None), value(0))
    )
    assert evaluate(indicator) is None
