from sdd.generic.plan_search import sequences, proposed_links
from sdd.generic.catalog import Catalog
from sdd.generic.planner import literals
from sdd.db import Database


def answer(probabilities, selection=None):
    result = {"probabilities": probabilities}
    if selection is not None:
        result["_selection"] = selection
    return result


def test_beam_keeps_lower_scoring_compatible_outputs_instead_of_duplicates():
    answers = {
        "output_0": answer({"name": 0.9, "none": 0.1}),
        "output_1": answer({"gain": 0.7, "loss": 0.25, "none": 0.05}),
        "output_2": answer({"gain": 0.8, "none": 0.19, "loss": 0.01}),
    }
    candidates = sequences(answers, "output", 3)
    assert candidates[0][0] == ["name", "loss", "gain"]
    assert all(len(keys) == len(set(keys)) for keys, _ in candidates)


def test_explicit_correction_cannot_be_replaced_by_search():
    answers = {"output_0": answer({"wrong": 0.99, "right": 0.01}, "right")}
    assert sequences(answers, "output", 1)[0][0] == ["right"]
    assert answers["output_0"]["probabilities"]["right"] == 0.01


def test_chinese_classifier_counts_contribute_literal_candidates():
    assert 2 in literals("每组取最高的两个项目")["numbers"]
    assert 3 in literals("展示三家公司的数值")["numbers"]


def test_proposed_link_requires_unique_target_and_overlap_without_altering_catalog():
    db = Database("sqlite://")
    db.initialize()
    catalog = Catalog(db)
    catalog.create("t", "devices", [{"device_id": 1}, {"device_id": 2}], primary_key=["device_id"])
    catalog.create(
        "t",
        "samples",
        [{"id": 1, "device_id": "1"}, {"id": 2, "device_id": "2"}],
        primary_key=["id"],
    )
    links = proposed_links(catalog, "t", catalog.model_catalog("t"), [])
    assert any(
        link.source == "samples" and link.target == "devices" and link.inferred for link in links
    )
    assert all(not row["links"] for row in catalog.list("t"))
    catalog.create("t", "unrelated", [{"id": 1, "device_id": "x"}], primary_key=["id"])
    links = proposed_links(catalog, "t", catalog.model_catalog("t"), [])
    assert all(link.source != "unrelated" for link in links)
    db.engine.dispose()


def test_repair_cannot_override_user_output_limit_or_distinct_choice():
    from sdd.generic.compositional import CompositionalPlanner
    from sdd.generic.relational import Program

    plan = Program("readings", {}, [], outputs=["name"], limit=5, distinct=False)
    assert CompositionalPlanner.respects_corrections(
        plan, {"limit": "5", "output_0": "name", "distinct": "no"}
    )
    assert not CompositionalPlanner.respects_corrections(plan, {"limit": "10"})
    assert not CompositionalPlanner.respects_corrections(plan, {"output_0": "value"})
    assert not CompositionalPlanner.respects_corrections(plan, {"distinct": "yes"})


def test_final_plan_binding_keeps_raw_distribution_and_is_not_a_human_override():
    from sdd.generic.planning_review import ReviewDecisions

    class Model:
        model = "test"

        def ask(self, tenant, state, questions):
            return {
                "model": self.model,
                "answers": {
                    "output_0": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 0.6, "b": 0.4},
                    }
                },
            }

    context = ReviewDecisions(Model())
    context.ask(
        "t",
        {},
        {
            "output_0": {
                "type": "choice",
                "instructions": "Output",
                "criteria": {"a": "first", "b": "second"},
            }
        },
    )
    plan = context.attach({"logical_sql": "SELECT 1", "_plan_bindings": {"output_0": "b"}})
    decision = plan["review"]["decisions"][0]
    assert decision["selected"] == "b" and decision["model_selected"] == "a"
    assert decision["probability"] == 0.4 and decision["model_probability"] == 0.6
    assert decision["selected_by"] == "relational_search" and not decision["overridden"]
    assert context.batches[0]["result"]["answers"]["output_0"]["probabilities"] == {
        "a": 0.6,
        "b": 0.4,
    }


def test_explicit_arithmetic_is_schema_bound_and_rejects_ambiguous_identifiers():
    from sdd.generic.plan_search import explicit_arithmetic
    from sdd.generic.relational import Field

    fields = {
        "a": Field("a", "samples", "reading", "number"),
        "b": Field("b", "samples", "sample_mass", "number"),
    }
    assert explicit_arithmetic("Add up reading times sample mass", fields) == ("a", "b", "product")
    assert explicit_arithmetic("reading除以sample_mass", fields) == ("a", "b", "ratio")
    assert explicit_arithmetic("reading was high several times", fields) is None
    fields["c"] = Field("c", "another", "reading", "number")
    assert explicit_arithmetic("reading times sample mass", fields) is None
