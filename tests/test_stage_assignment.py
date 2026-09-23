from itertools import permutations
import math

import pytest

from sdd.generic.assignment import assign_unique


def test_assignment_matches_exhaustive_optimum_without_greedy_duplicates():
    rows = [{"a": 0.55, "b": 0.4, "c": 0.05}, {"a": 0.8, "b": 0.1, "c": 0.1}]
    expected = max(
        permutations("abc", 2),
        key=lambda picks: sum(math.log(rows[i][key]) for i, key in enumerate(picks)),
    )
    assert tuple(assign_unique(rows)) == expected == ("b", "a")


def test_required_outputs_and_user_corrections_constrain_assignment():
    rows = [{"a": 0.8, "b": 0.19, "c": 0.01}, {"a": 0.7, "b": 0.25, "c": 0.05}]
    assert assign_unique(rows, required={"c"}, fixed={0: "b"}) == ["b", "c"]
    with pytest.raises(ValueError, match="conflict"):
        assign_unique(rows, fixed={0: "a", 1: "a"})
    with pytest.raises(ValueError, match="cover"):
        assign_unique(rows, required={"a", "b", "c"})


def test_assignment_does_not_mutate_model_probabilities():
    rows = [{"a": 0.9, "b": 0.1}, {"a": 0.8, "b": 0.2}]
    snapshot = [dict(row) for row in rows]
    assert assign_unique(rows) == ["a", "b"]
    assert rows == snapshot
