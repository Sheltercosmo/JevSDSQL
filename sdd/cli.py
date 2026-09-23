import argparse
import json
import os
import time
from pathlib import Path
from .config import runtime


def main():
    parser = argparse.ArgumentParser(description="Self-Developing Database")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("demo")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    worker = sub.add_parser("worker")
    worker.add_argument("--tenant", required=True)
    worker.add_argument("--once", action="store_true")
    feature_worker = sub.add_parser("feature-worker", help="Refresh maintained generic features")
    feature_worker.add_argument("--tenant", required=True)
    feature_worker.add_argument("--once", action="store_true")
    maintenance = sub.add_parser("maintain")
    maintenance.add_argument("--tenant", required=True)
    maintenance.add_argument("--once", action="store_true")
    query = sub.add_parser("query")
    query.add_argument("plan")
    query.add_argument("--tenant", required=True)
    natural = sub.add_parser("ask", help="Ask a natural-language question")
    natural.add_argument("question")
    natural.add_argument("--tenant")
    natural.add_argument("--preview", action="store_true")
    natural.add_argument("--max-evaluations", type=int, default=100)
    natural.add_argument(
        "--dataset", action="append", help="Dataset name or ID; repeat to select multiple"
    )
    live = sub.add_parser("live-test")
    live.add_argument("--model", required=True)
    live.add_argument("--output", default="artifacts/live-test.json")
    args = parser.parse_args()
    db, executor = runtime()
    if args.command == "init":
        db.initialize()
        print("Database initialized.")
    elif args.command == "demo":
        from .demo import demo

        db.initialize()
        print(json.dumps(demo(db), indent=2))
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("sdd.api:create_app", factory=True, host="127.0.0.1", port=args.port)
    elif args.command == "worker":
        while True:
            worked = executor.workers.work_one(args.tenant)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    elif args.command == "feature-worker":
        from .generic.api import services
        from .generic.features import FeatureRegistry

        _, service, _ = services(executor)
        if service.decisions is None:
            parser.error("Configure TYPESAFE_API_KEY to maintain semantic features")
        registry = FeatureRegistry(db)
        while True:
            worked = registry.work_one(args.tenant, service.decisions)
            if args.once:
                break
            if not worked:
                time.sleep(1)
    elif args.command == "maintain":
        from datetime import datetime, timezone
        from . import schema as s
        from .maintenance import refresh

        while True:
            for m in executor.ledger.list(args.tenant, s.materializations):
                c = executor.ledger.get(args.tenant, s.concepts, m["concept_id"])
                if c["status"] != "active":
                    continue
                if m["last_run"]:
                    last = executor.ledger.get(args.tenant, s.runs, m["last_run"])
                    age = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])
                    ).total_seconds()
                    if age < m["freshness_seconds"]:
                        continue
                refresh(executor, args.tenant, m["id"])
            if args.once:
                break
            time.sleep(30)
    elif args.command == "query":
        print(
            json.dumps(
                executor.execute(args.tenant, json.loads(Path(args.plan).read_text())), indent=2
            )
        )
    elif args.command == "ask":
        from .generic.api import services

        principals = list(json.loads(os.getenv("SDD_API_TOKENS", "{}")).values())
        tenants = {p["tenant"] for p in principals}
        tenant = args.tenant or (next(iter(tenants)) if len(tenants) == 1 else None)
        if not tenant:
            parser.error(
                "Specify --tenant when local credentials identify zero or multiple tenants."
            )
        owner = next((p["name"] for p in principals if p["tenant"] == tenant), "local-cli")
        _, service, planner = services(executor)
        if planner is None:
            parser.error("Configure TYPESAFE_API_KEY to enable Jev planning.")
        from .generic.planning_review import PlanReviews, PlanReviewRequired
        from .generic.history import QueryHistory

        def execute_question():
            plan = PlanReviews(db, planner).begin(tenant, owner, args.question, args.dataset)
            if args.preview:
                return {"plan": plan, "executed": False}
            try:
                return service.execute(
                    tenant,
                    plan["logical_sql"],
                    plan=plan,
                    request=args.question,
                    actor=owner,
                    max_evaluations=args.max_evaluations,
                )
            except Exception as exc:
                exc.query_plan = plan
                raise

        try:
            result = QueryHistory(db).capture(
                tenant,
                owner,
                {
                    "mode": "natural",
                    "text": args.question,
                    "dataset_ids": args.dataset,
                    "max_evaluations": args.max_evaluations,
                },
                execute_question,
            )
        except PlanReviewRequired as exc:
            result = {
                "review_required": True,
                "executed": False,
                "detail": str(exc),
                "plan": exc.plan,
                "history_id": exc.history_id,
            }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.command == "live-test":
        from .demo import seed

        db.initialize()
        _, plan = seed(db, tenant="live-test", provider="jev", model=args.model)
        cold = executor.execute("live-test", plan)
        warm = executor.execute("live-test", plan)
        output = {"cold": cold, "warm": warm, "same_result": cold["result"] == warm["result"]}
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "report": str(path),
                    "same_result": output["same_result"],
                    "cold_coverage": cold["manifest"],
                    "warm_coverage": warm["manifest"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
