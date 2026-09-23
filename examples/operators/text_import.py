"""Describe columns and rows once; extract a document, optionally importing its entries."""

import argparse
import json
import os
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Import automatically when all required values resolve",
    )
    parser.add_argument("--name", default="delivery_notes", help="New dataset name")
    args = parser.parse_args()
    token = os.environ.get("SDD_TOKEN")
    if not token:
        parser.error("Set SDD_TOKEN to a database reviewer token")
    body = {
        "name": args.name,
        "row_description": "One supplier delivery per row",
        "record_mode": "line",
        "columns": [
            {"name": "supplier", "type": "text", "description": "Supplier name"},
            {"name": "quantity", "type": "integer", "description": "Number of valves"},
            {"name": "amount", "type": "number", "description": "Total charge in dollars"},
        ],
        "text": "Northwind sent 12 valves, total $1,250.50.\nEastbank sent 8 valves, total $840.00.",
        "commit": args.commit,
    }
    base = os.environ.get("SDD_URL", "http://127.0.0.1:8000").rstrip("/")
    request = urllib.request.Request(
        base + "/data/extractions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
