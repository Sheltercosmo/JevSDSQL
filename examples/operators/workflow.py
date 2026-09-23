"""Demonstrate a skipped conditional stage without making a model request."""

import argparse
import os

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.getenv("SDD_TOKEN")
    if not token:
        parser.error("Set SDD_TOKEN to your database API token")

    request = {
        "operator": "JEV.WORKFLOW",
        "arguments": {
            "stages": [
                {"id": "gate", "literal": False},
                {
                    "id": "stage2",
                    "when": {"stage": "gate", "equals": True},
                    "literal": "This branch was selected",
                },
            ]
        },
        "limits": {"max_judgments": 0, "max_requests": 0},
    }
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        response = client.post(
            "/jev/call", headers={"Authorization": f"Bearer {token}"}, json=request
        )
        response.raise_for_status()
    result = response.json()
    if result["output_state"] != "VALUE":
        raise SystemExit(f"Workflow did not complete: {result['operation_state']}")
    for name, stage in result["value"]["stages"].items():
        print(f"{name}: {stage['output_state']} / {stage['operation_state']} / {stage['value']}")


if __name__ == "__main__":
    main()
