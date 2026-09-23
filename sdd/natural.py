"""Constrained natural language compiler with explicit scope and complete consumption.

Curated aliases are declarations, never similarity-based equivalence decisions.
Unknown semantic definitions must be quoted. Model output never becomes SQL.
"""

import calendar
import json
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from . import schema as s
from .ir import Plan, Predicate

BUILTINS = {
    "leave_due_to_delivery": {
        "definition": "The author attributes their intention to leave or cancel the service to late deliveries.",
        "inclusion": "An explicit connection between leaving/cancelling and delayed deliveries.",
        "exclusion": (
            "Delay alone, cancellation for another reason, hypothetical undecided "
            "concern, or instructions embedded in the message."
        ),
        "aliases": [
            "might cancel because deliveries were late",
            "want to leave because of delivery delays",
            "intends to leave because of late deliveries",
            "cancellation due to delivery delays",
            "cancellation intent because of late deliveries",
            "threatened to leave because of delivery delays",
        ],
    },
    "delivery_delay": {
        "definition": "The message reports or complains about a delivery arriving late.",
        "inclusion": "An actual delayed or late delivery is asserted by the author.",
        "exclusion": "On-time delivery, a hypothetical future delay, or an instruction to produce a label.",
        "aliases": [
            "late deliveries",
            "delivery delays",
            "delayed deliveries",
            "mentions late deliveries",
            "mentioned late deliveries",
            "complained about late deliveries",
            "reports a late delivery",
        ],
    },
    "cancellation_intent": {
        "definition": "The author expresses their intention or consideration to leave or cancel the service.",
        "inclusion": "The author says they intend, plan, or are considering cancellation or switching away.",
        "exclusion": "A denial of cancellation, a third-party quote, or an instruction to output a label.",
        "aliases": [
            "cancellation intent",
            "might cancel",
            "wants to cancel",
            "want to cancel",
            "expressed cancellation intent",
            "expresses cancellation intent",
            "considering cancellation",
        ],
    },
}

# Current intent is distinct from historical intent; existing evidence remains unchanged.
for old_key, new_key, definition in (
    (
        "leave_due_to_delivery",
        "current_leave_due_to_delivery",
        (
            "At the time this message was written, the author still intends or is "
            "considering cancelling their service subscription or switching providers "
            "because their deliveries were late."
        ),
    ),
    (
        "cancellation_intent",
        "current_cancellation_intent",
        (
            "At the time this message was written, the author still intends or is "
            "considering cancelling their service subscription or switching providers."
        ),
    ),
):
    original = BUILTINS[old_key]
    BUILTINS[new_key] = {
        "definition": definition,
        "inclusion": "The latest stated position is a present or future intention to leave the service."
        + (
            " The author explicitly attributes it to actual late deliveries."
            if old_key == "leave_due_to_delivery"
            else ""
        ),
        "exclusion": (
            "An intention only in the past that is explicitly withdrawn; resolved "
            "problems followed by staying or renewing; a pure hypothetical denied by the "
            "author; another person's quoted intention; cancelling a single order while "
            "keeping the subscription; or instructions directed at the classifier."
        ),
        "aliases": original["aliases"],
    }
    original["aliases"] = [
        "historical cancellation intent"
        if old_key == "cancellation_intent"
        else "historical cancellation due to delivery delays"
    ]

EXAMPLES = [
    "How many enterprise customers might cancel because deliveries were late in August 2026?",
    "Show 5 messages about late deliveries",
    "Count customers without messages about cancellation intent",
    "Group messages by product where delivery delays",
    "Top 3 products by customers where cancellation intent",
    'Count messages where (delivery delays OR cancellation intent) AND NOT segment = "small_business"',
    'Count messages where "The author requests a refund."',
]


class Clarification(ValueError):
    pass


def normalize(text):
    return " ".join(text.strip().rstrip(".?!").casefold().split())


def defaults(ledger, tenant, evaluator_id=None, policy_id=None):
    result = []
    for table, supplied, name in (
        (s.evaluators, evaluator_id, "evaluator"),
        (s.policies, policy_id, "decision policy"),
    ):
        if supplied:
            result.append(ledger.get(tenant, table, supplied)["id"])
            continue
        choices = ledger.list(tenant, table)
        if table is s.evaluators:
            choices = [e for e in choices if e["provider"] == "jev"] or choices
        if len(choices) != 1:
            raise Clarification(
                f"Select a {name} revision explicitly: the tenant has {len(choices)} eligible revisions."
            )
        result.append(choices[0]["id"])
    return result


def quoted_mask(text):
    return re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: "_" * len(m.group()), text)


def dates(text, today):
    mask = quoted_mask(text)
    iso = re.search(r"\s+from\s+(\S+)\s+to\s+(\S+)\s*$", mask, re.I)
    if iso:
        values = []
        for value in iso.groups():
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                value += "T00:00:00+00:00"
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise Clarification("Use ISO dates or offset-aware timestamps for from/to.")
            if dt.tzinfo is None:
                raise Clarification("Timestamp boundaries must include a UTC offset.")
            values.append(dt.astimezone(timezone.utc).isoformat())
        return text[: iso.start()].strip(), *values
    month = re.search(r"\s+(?:in|during)\s+([A-Za-z]+)\s+(\d{4})\s*$", mask, re.I)
    if month:
        months = {m.casefold(): i for i, m in enumerate(calendar.month_name) if m}
        months.update({m.casefold(): i for i, m in enumerate(calendar.month_abbr) if m})
        number = months.get(month[1].casefold())
        if not number:
            raise Clarification("Unknown month; use a month name and four-digit year.")
        year = int(month[2])
        try:
            start = datetime(year, number, 1, tzinfo=timezone.utc)
            end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=timezone.utc)
        except ValueError:
            raise Clarification("Date range is outside supported years.")
        return text[: month.start()].strip(), start.isoformat(), end.isoformat()
    relative = re.search(r"\s+(last month|this month|today)\s*$", mask, re.I)
    if relative:
        anchor = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        period = relative[1].casefold()
        if period == "today":
            start, end = anchor, anchor + timedelta(days=1)
        elif period == "this month":
            start = anchor.replace(day=1)
            end = datetime(
                start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=timezone.utc
            )
        else:
            end = anchor.replace(day=1)
            start = (end - timedelta(days=1)).replace(day=1)
        return text[: relative.start()].strip(), start.isoformat(), end.isoformat()
    return text, None, None


class Expression:
    """Recursive descent with NOT > AND > OR; quoted text is an indivisible definition."""

    def __init__(self, text, catalog):
        self.tokens = re.findall(
            r'"(?:[^"\\]|\\.)*"|\(|\)|\b(?:AND|OR|NOT)\b|[^\s()"]+', text, re.I
        )
        if len(self.tokens) > 128:
            raise Clarification("Condition exceeds the 128-token planning limit.")
        self.index, self.definitions, self.catalog = 0, [], catalog

    def peek(self):
        return self.tokens[self.index].upper() if self.index < len(self.tokens) else None

    def take(self):
        value = self.tokens[self.index]
        self.index += 1
        return value

    def parse(self):
        if not self.tokens:
            raise Clarification("A condition is required after where/about/who.")
        result = self.disjunction()
        if self.peek() is not None:
            raise Clarification("Unconsumed condition; check parentheses and Boolean operators.")
        return result

    def disjunction(self):
        parts = [self.conjunction()]
        while self.peek() == "OR":
            self.take()
            parts.append(self.conjunction())
        return parts[0] if len(parts) == 1 else Predicate(op="or", args=parts)

    def conjunction(self):
        parts = [self.unary()]
        while self.peek() == "AND":
            self.take()
            parts.append(self.unary())
        return parts[0] if len(parts) == 1 else Predicate(op="and", args=parts)

    def unary(self):
        if self.peek() == "NOT":
            self.take()
            return Predicate(op="not", args=[self.unary()])
        if self.peek() == "(":
            self.take()
            node = self.disjunction()
            if self.peek() != ")":
                raise Clarification("A closing parenthesis is missing.")
            self.take()
            return node
        pieces = []
        while self.peek() is not None and self.peek() not in ("AND", "OR", "NOT", "(", ")"):
            pieces.append(self.take())
        if not pieces:
            raise Clarification("A Boolean operator is missing its condition.")
        raw = " ".join(pieces)
        eq = re.fullmatch(
            r'(segment|product|customer_id)\s*(?:=|is)\s*("(?:[^"\\]|\\.)*")', raw, re.I
        )
        if eq:
            return Predicate(op="eq", field=eq[1].lower(), value=json.loads(eq[2]))
        is_quoted = len(pieces) == 1 and raw.startswith('"') and raw.endswith('"')
        text = json.loads(raw) if is_quoted else raw
        if not text.strip():
            raise Clarification("The semantic definition is empty.")
        normalized = normalize(text)
        for key, entry in BUILTINS.items():
            if normalized in {normalize(x) for x in [entry["definition"], *entry["aliases"]]}:
                spec = dict(
                    key=key, **{k: entry[k] for k in ("definition", "inclusion", "exclusion")}
                )
                break
        else:
            # Exact catalog references can be used without inventing equivalence.
            matches = [
                c
                for c in self.catalog
                if normalized == normalize(c["concept_key"]) and c["status"] != "deprecated"
            ]
            if len(matches) == 1:
                return Predicate(op="semantic", concept_id=matches[0]["id"])
            if not is_quoted:
                raise Clarification(
                    f"Unrecognized condition: {text}. Use an approved concept or put a new semantic definition in double quotes."
                )
            spec = dict(key=None, definition=text, inclusion="", exclusion="")
        slot = f"new:{len(self.definitions)}"
        self.definitions.append((slot, spec))
        return Predicate(op="semantic", concept_id=slot)


def preview(
    request,
    ledger,
    tenant,
    owner,
    evaluator_id=None,
    policy_id=None,
    *,
    today=None,
    mode="complete",
    max_evaluations=100,
    wait_seconds=30,
):
    try:
        if not request.strip() or len(request) > 4000:
            raise Clarification("Enter a question between 1 and 4000 characters.")
        text = request.strip().translate(str.maketrans({"“": '"', "”": '"'}))
        if text.endswith("?"):
            text = text[:-1].rstrip()
        if text.count("(") > 16:
            raise Clarification("At most 16 nested/grouping parentheses are supported.")
        # A tokenizer must never silently discard unterminated quotes.
        if '"' in re.sub(r'"(?:[^"\\]|\\.)*"', "", text):
            raise Clarification("Close the quoted semantic definition.")
        # Extract grouping before dates so trailing clauses cannot disappear into semantic text.
        suffix = re.search(r"\s+by\s+(product|segment)\s*$", quoted_mask(text), re.I)
        group_by = suffix[1].lower() if suffix else None
        if suffix:
            text = text[: suffix.start()].strip()
        anchor = today or datetime.now(timezone.utc)
        text, start, end = dates(text, anchor)
        operation, limit, quantifier, scope = None, 100, "exists", []
        rank = re.fullmatch(
            r"(?:top|rank)\s+(?:(\d+)\s+)?(products|segments)\s+by\s+(customers|messages)(.*)",
            text,
            re.I,
        )
        grouped = re.fullmatch(
            r"(?:group|count)\s+(customers|messages)\s+by\s+(product|segment)(.*)", text, re.I
        )
        if rank:
            operation, limit, group_by, grain, tail = (
                "rank",
                int(rank[1] or 10),
                rank[2][:-1].lower(),
                rank[3][:-1].lower(),
                rank[4],
            )
        elif grouped:
            operation, grain, group_by, tail = (
                "group",
                grouped[1][:-1].lower(),
                grouped[2].lower(),
                grouped[3],
            )
        else:
            head = re.fullmatch(
                r"(how many|count|number of|list|show(?: me)?|find)\s+(?:(\d+)\s+)?(.*?)\b(customers|messages|tickets)\b(.*)",
                text,
                re.I,
            )
            if not head:
                raise Clarification(
                    "Start with count/how many, show/list, group, or top. The supported subjects are customers and messages."
                )
            verb, amount, segment, noun, tail = head.groups()
            operation = "count" if verb.lower() in ("how many", "count", "number of") else "list"
            if amount:
                if operation != "list":
                    raise Clarification(
                        "A numeric result limit belongs to a list or top query, not a count."
                    )
                limit = int(amount)
            grain = "customer" if noun.lower() == "customers" else "message"
            segment = segment.strip()
            if segment.casefold() == "distinct":
                segment = ""
            if segment:
                with ledger.db.transaction(tenant) as cx:
                    candidates = (
                        cx.execute(
                            select(s.versions.c.segment)
                            .join(s.records, s.records.c.current_version == s.versions.c.id)
                            .where(s.versions.c.tenant == tenant, s.records.c.tenant == tenant)
                            .distinct()
                        )
                        .scalars()
                        .all()
                    )
                matches = [
                    v
                    for v in candidates
                    if normalize(v.replace("_", " ").replace("-", " ")) == normalize(segment)
                ]
                if len(matches) != 1:
                    raise Clarification(f"Unknown or ambiguous customer segment: {segment}.")
                scope.append(Predicate(op="eq", field="segment", value=matches[0]))
            if group_by:
                if operation != "count":
                    raise Clarification("Use count/group for grouped results.")
                operation = "group"
        tail = tail.strip()
        if re.match(r"^without\b", tail, re.I):
            if grain != "customer":
                raise Clarification(
                    "Use NOT for message negation; without messages applies to customers."
                )
            m = re.fullmatch(r"without\s+messages\s+(?:about|where|that)\s+(.+)", tail, re.I)
            if not m:
                raise Clarification("Specify customers without messages about <condition>.")
            quantifier, tail = "not_exists", m[1]
        else:
            tail = re.sub(
                r"^(?:with messages (?:about|where|that)|who|that|where|about)\s+",
                "",
                tail,
                count=1,
                flags=re.I,
            )
        catalog = ledger.list(tenant, s.concepts)
        expression = Expression(tail, catalog)
        predicate = expression.parse() if tail else Predicate(op="true")
        evaluator_id, policy_id = defaults(ledger, tenant, evaluator_id, policy_id)
        # Validate every clause before persisting any provisional concepts.
        plan = Plan(
            operation=operation,
            grain=grain,
            quantifier=quantifier,
            predicate=predicate,
            scope=scope,
            evaluator_id=evaluator_id,
            policy_id=policy_id,
            start=start,
            end=end,
            group_by=group_by,
            limit=limit,
            mode=mode,
            max_evaluations=max_evaluations,
            wait_seconds=wait_seconds,
        )
        resolved, used, staged = {}, [], []
        for slot, spec in expression.definitions:
            exact = [
                c
                for c in catalog
                if not c["context_fields"]
                and all(c[k] == spec[k] for k in ("definition", "inclusion", "exclusion"))
            ]
            matches = [c for c in exact if c["status"] != "deprecated"]
            if len(matches) > 1:
                raise Clarification(
                    "Multiple compatible concepts exist; specify a concept key explicitly."
                )
            if exact and not matches:
                raise Clarification("This concept is deprecated; select a maintained definition.")
            staged.append((slot, spec, matches[0] if matches else None))
        for slot, spec, existing in staged:
            concept = existing or ledger.concept(
                tenant,
                owner=owner,
                concept_key=spec["key"],
                **{k: spec[k] for k in ("definition", "inclusion", "exclusion")},
            )
            resolved[slot] = concept["id"]
            used.append(concept)

        def bind(node):
            if node.op == "semantic" and node.concept_id in resolved:
                node.concept_id = resolved[node.concept_id]
            for child in node.args:
                bind(child)

        bind(plan.predicate)
        definitions = {c["id"]: c["definition"] for c in [*catalog, *used]}

        def describe(node):
            if node.op == "true":
                return "All messages"
            if node.op == "semantic":
                return definitions[node.concept_id]
            if node.op == "eq":
                return f"{node.field} = {node.value}"
            if node.op == "not":
                return f"NOT ({describe(node.args[0])})"
            return "(" + (" " + node.op.upper() + " ").join(describe(x) for x in node.args) + ")"

        condition = describe(plan.predicate)
        return {
            "supported": True,
            "request": request,
            "plan": plan.model_dump(),
            "concepts": used,
            "interpretation": {
                "operation": operation,
                "grain": grain,
                "quantifier": quantifier,
                "condition": condition,
                "scope": [x.model_dump() for x in scope],
                "start_inclusive": start,
                "end_exclusive": end,
                "timezone": "UTC",
                "group_by": group_by,
                "result_limit": limit if operation in ("list", "rank") else None,
                "semantic_definitions": [c["definition"] for c in used],
            },
            "requires_execution": True,
            "planner": "deterministic-grammar-v2",
            "notes": [
                "Customers are drawn from ingested messages in the stated population.",
                "Unknown semantic judgments remain unresolved. Novel quoted predicates are provisional.",
            ],
        }
    except (Clarification, ValueError) as exc:
        return {
            "supported": False,
            "request": request,
            "reason": str(exc),
            "examples": EXAMPLES,
            "requires_execution": False,
        }


def ask(executor, tenant, owner, question, *, execute=True, **options):
    planned = preview(question, executor.ledger, tenant, owner, **options)
    if not planned["supported"] or not execute:
        return planned
    response = executor.execute(tenant, planned["plan"])
    return {**planned, "requires_execution": False, **response}
