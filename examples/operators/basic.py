"""Call a semantic operator through the local database API."""

import argparse
import json
import os

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--text", default="The requested work was completed yesterday.")
    args = parser.parse_args()
    token = os.getenv("SDD_TOKEN")
    if not token:
        parser.error("Set SDD_TOKEN to your database API token")

    request = {
        "operator": "JEV.NOUL",
        "arguments": {
            "state": args.text,
            "proposition": "The source reports completed work, not just a promise or request.",
        },
        "limits": {"max_judgments": 1, "max_requests": 1},
    }
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        response = client.post(
            "/jev/call", headers={"Authorization": f"Bearer {token}"}, json=request
        )
        response.raise_for_status()
    result = response.json()
    print(f"{result['output_state']} / {result['operation_state']}")
    if result["output_state"] == "VALUE":
        print(json.dumps(result["value"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result["observations"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
