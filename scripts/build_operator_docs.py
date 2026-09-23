"""Render the function reference from the examples exposed by the API."""

import argparse
import inspect
import json
from pathlib import Path

from sdd.operators.service import OperatorService
from sdd.operators.usage import examples

DESTINATION = Path(__file__).resolve().parents[1] / "docs" / "JEV_FUNCTION_REFERENCE.md"
INTRODUCTION = """# JEV function reference

Each section gives the `arguments` object for `POST /jev/call`. Set `operator` to the section name:

```json
{
  "operator": "JEV.NOUL",
  "arguments": {
    "state": "The work is complete.",
    "proposition": "The source reports completed work."
  }
}
```

Use your database bearer token. Most examples run on supplied text; examples with `<placeholders>` require IDs from earlier operations. All calls retain a run/evidence record. REVIEW, PROMOTE, MATERIALIZE and REFRESH require a reviewer token; DISCOVER saves provisional definitions.

Inspect `output_state` before reading `value`. Incomplete results use `partial_value`; observations keep their individual output and operation states. Model-dependent examples illustrate inputs and result shapes, not guaranteed labels. Shared budgets, authorization and state rules are in the [operator guide](JEV_OPERATORS.md).

Optional execution controls belong beside `operator` and `arguments`, for example `"limits": {"max_judgments": 100, "concurrency": 4}`. Do not pass the outer budget as an operator argument. Most population parameters also accept `{"dataset_id":"...","where":{"team":"North"}}`; registered datasets require a primary key. Direct states for NOUL, CHOICE, SCORE and COMPARE are literal input values.

The same complete request examples are available from `GET /jev/operators/examples` and each catalog entry's `usage` field. The source is [examples.json](../sdd/operators/examples.json); rebuild this reference with `python -m scripts.build_operator_docs`.

"""


def render():
    entries = examples()
    sections = [
        INTRODUCTION,
        " · ".join(
            f"[{entry['operator'].removeprefix('JEV.')}](#{entry['operator'].lower().replace('.', '')})"
            for entry in entries
        ),
        "\n",
    ]
    for entry in entries:
        name = entry["operator"].removeprefix("JEV.").lower()
        signature = inspect.signature(getattr(OperatorService, name))
        signature = signature.replace(
            parameters=[
                parameter for key, parameter in signature.parameters.items() if key != "self"
            ]
        )
        sections.append(f"## {entry['operator']}\n\n{entry['purpose']}\n\n`{name}{signature}`\n")
        if entry.get("when_to_use"):
            sections.append(f"{entry['when_to_use']}\n")
        if entry.get("prerequisite"):
            sections.append(f"Requires: {entry['prerequisite']}\n")
        arguments = json.dumps(entry["request"]["arguments"], ensure_ascii=False, indent=2)
        sections.append(f"```json\n{arguments}\n```\n\nReturns: {entry['returns']}\n")
    return "\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Check without rewriting the reference"
    )
    args = parser.parse_args()
    document = render()
    if args.check:
        if not DESTINATION.exists() or DESTINATION.read_text(encoding="utf-8") != document:
            raise SystemExit(
                "Function reference is stale; run python -m scripts.build_operator_docs"
            )
        print("Function reference matches the API examples.")
    else:
        DESTINATION.write_text(document, encoding="utf-8")
        print("Updated docs/JEV_FUNCTION_REFERENCE.md")


if __name__ == "__main__":
    main()
