import copy
import httpx
import pytest
from sdd.db import Database
from sdd.evaluators import JevBackend, ProviderError
from sdd.generic.jev import Decisions, choice, noul, selected
from sdd.generic.planner import literals


def setup(tmp_path, payload, status=200):
    db = Database("sqlite:///" + str(tmp_path / "choices.db"))
    db.initialize()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    )
    return Decisions(db, JevBackend("fixture-key", client))


VALID = {
    "model": "jev-1.13.0",
    "answers": {"q": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}}},
    "usage": {"input_tokens": 10, "output_tokens": 1},
}


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.update(model="jev-latest"),
        lambda x: x["answers"]["q"].update(choice="outside"),
        lambda x: x["answers"]["q"].update(probabilities={"a": 0.9, "b": 0.9}),
        lambda x: x["answers"]["q"].update(probabilities={"a": True, "b": 0}),
        lambda x: x["answers"].update(extra={"type": "noul", "noul": 1}),
        lambda x: x.update(usage={"input_tokens": -2}),
    ],
)
def test_invalid_provider_decisions_are_rejected(tmp_path, change):
    p = copy.deepcopy(VALID)
    change(p)
    d = setup(tmp_path, p)
    with pytest.raises(ProviderError, match="InvalidDecisionResponse"):
        d.ask("a", {}, {"q": choice("test", {"a": "A", "b": "B"})})


def test_choice_uncertainty_and_transport_failure(tmp_path):
    with pytest.raises(ValueError):
        selected({"choice": "a", "probabilities": {"a": 0.5, "b": 0.5}})
    d = setup(tmp_path, {}, 429)
    with pytest.raises(ProviderError) as e:
        d.ask("a", {}, {"q": noul("test")})
    assert e.value.retryable and e.value.code == "HTTP_429"


def test_chinese_numbers_dates_and_quoted_literals():
    x = literals("数量至少一百、价格小于二十；名称改为“新值”，2026年9月")
    assert 100 in x["numbers"] and 20 in x["numbers"] and "新值" in x["strings"]
    assert x["month_ranges"] == [("2026-09-01", "2026-10-01")]
