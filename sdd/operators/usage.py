"""Usage examples shared by the API catalog and function reference."""

import json
from importlib.resources import files


def examples():
    path = files("sdd.operators").joinpath("examples.json")
    return json.loads(path.read_text(encoding="utf-8"))


def by_operator():
    return {example["operator"]: example for example in examples()}
