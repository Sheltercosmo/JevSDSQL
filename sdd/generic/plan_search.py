"""Constrained beam search and evidence-backed, explicitly proposed read joins."""

from copy import deepcopy
import math
import re

from sqlalchemy import select, func, text
from sqlalchemy.exc import SQLAlchemyError
import time

from .relational import Link


def alternatives(answer, count=3):
    if "_selection" in answer:
        return [(answer["_selection"], 1.0)]
    return sorted(answer["probabilities"].items(), key=lambda item: -item[1])[:count]


def sequences(answers, prefix, length, width=6, required=False):
    """Keep unique, contiguous output roles; marginal scores only rank alternatives."""
    beam = [([], 0.0, False)]
    for index in range(length):
        expanded = []
        for values, score, ended in beam:
            for key, probability in alternatives(answers[f"{prefix}_{index}"]):
                if (
                    (ended and key != "none")
                    or (key in values)
                    or ((required or (prefix == "output" and index == 0)) and key == "none")
                ):
                    continue
                expanded.append(
                    (
                        values + ([key] if key != "none" else []),
                        score + math.log(max(probability, 0.000001)),
                        ended or key == "none",
                    )
                )
        beam = sorted(expanded, key=lambda item: -item[1])[:width]
    return [(keys, score) for keys, score, _ in beam]


def program_variants(program, answers, width=12, output_count=None):
    outputs = sequences(
        answers, "output", output_count or 6, width=6, required=output_count is not None
    )
    orders = sequences(answers, "sort", 3, width=3)
    combinations = sorted(
        ((output, order, a + b) for output, a in outputs for order, b in orders),
        key=lambda item: -item[2],
    )[:width]
    variants = []
    for output, order, score in combinations:
        candidate = deepcopy(program)
        candidate.outputs = output
        candidate.order = [
            (key, alternatives(answers[f"direction_{index}"], 1)[0][0] == "desc")
            for index, key in enumerate(order)
        ]
        if candidate.aggregation != "value":
            candidate.groups = list(
                dict.fromkeys(
                    [key for key in output if key in candidate.fields]
                    + ([candidate.partition] if candidate.partition else [])
                )
            )
        variants.append(("Compatible output/sort contract", candidate))
    if "_selection" not in answers["distinct"]:
        for description, candidate in list(variants):
            alternative = deepcopy(candidate)
            alternative.distinct = not candidate.distinct
            variants.append(
                (
                    description
                    + (
                        "; preserve duplicates"
                        if not alternative.distinct
                        else "; unique output rows"
                    ),
                    alternative,
                )
            )
    return variants or [("Selected contract", program)]


def words(name):
    return {
        token[:-3] + "y" if token.endswith("ies") else token.rstrip("s")
        for token in re.findall(r"[a-z]+", name.casefold())
    } - {"id", "code", "key", "list", "data", "table"}


def proposed_links(catalog, tenant, datasets, approved):
    """Read-only hypotheses; observed overlap never promotes them to foreign keys."""
    known = {
        (link.source, link.source_column, link.target, link.target_column) for link in approved
    }
    domains, unique = {}, {}
    started = time.perf_counter()

    def profile(dataset, column):
        key = dataset["name"], column["name"]
        if key not in domains:
            if len(domains) >= 32 or time.perf_counter() - started > 1.5:
                return set(), False
            with catalog.db.transaction(tenant) as connection:
                if catalog.db.engine.dialect.name == "postgresql":
                    connection.execute(text("SET LOCAL statement_timeout = '500ms'"))
                source = catalog.table(dataset, connection).c[column["name"]]
                values = (
                    connection.execute(
                        select(source)
                        .where(source.is_not(None))
                        .distinct()
                        .order_by(source)
                        .limit(513)
                    )
                    .scalars()
                    .all()
                )
                count, distinct = connection.execute(
                    select(func.count(source), func.count(func.distinct(source)))
                ).one()
            domains[key] = {str(value) for value in values}
            unique[key] = count == distinct and bool(count)
        return domains[key], unique[key]

    result = []
    for source in datasets:
        for target in datasets:
            if source["name"] == target["name"]:
                continue
            for left in source["columns"]:
                if left.get("feature_id"):
                    continue
                for right in target["columns"]:
                    if right.get("feature_id"):
                        continue
                    identity = source["name"], left["name"], target["name"], right["name"]
                    if (
                        identity in known
                        or (identity[2], identity[3], identity[0], identity[1]) in known
                    ):
                        continue
                    exact = left["name"].casefold() == right["name"].casefold() and left[
                        "name"
                    ].casefold() not in (
                        "id",
                        "name",
                        "type",
                        "value",
                        "year",
                        "date",
                    )
                    named_target = (
                        bool(words(left["name"]) & words(target["name"]))
                        and right["name"] in target["primary_key"]
                        and len(target["primary_key"]) == 1
                    )
                    if (
                        left["name"] in source["primary_key"]
                        and right["name"] not in target["primary_key"]
                    ):
                        continue
                    if not (exact or named_target):
                        continue
                    if left["type"] not in ("text", "integer", "number") or right["type"] not in (
                        "text",
                        "integer",
                        "number",
                    ):
                        continue
                    try:
                        right_values, is_unique = profile(target, right)
                        if not is_unique:
                            continue
                        left_values, _ = profile(source, left)
                    except SQLAlchemyError:
                        continue
                    if not left_values or len(left_values & right_values) / len(left_values) < 0.8:
                        continue
                    differing = left["type"] != right["type"]
                    result.append(
                        Link(
                            source["name"],
                            target["name"],
                            left["name"],
                            right["name"],
                            inferred=True,
                            cast_source=differing,
                            cast_target=differing,
                        )
                    )
                    known.add(identity)
                    if len(result) >= 16:
                        return result
    return result


def explicit_arithmetic(question, fields):
    """Recognize only unambiguous, adjacent physical field names and operators."""
    operators = {
        "product": r"times|multiplied by|乘以|[×*]",
        "sum": r"plus|加上|[+]",
        "difference": r"minus|减去|[-]",
        "ratio": r"divided by|除以|[/÷]",
    }
    found = set()
    numeric = [
        (key, field)
        for key, field in fields.items()
        if field.kind in ("number", "integer") and not field.feature_id
    ]
    for left, a in numeric:
        for right, b in numeric:
            if left == right:
                continue
            lhs = r"[ _]+".join(re.escape(part) for part in a.name.split("_"))
            rhs = r"[ _]+".join(re.escape(part) for part in b.name.split("_"))
            for operation, operator in operators.items():
                if re.search(
                    r"(?<![A-Za-z0-9_])"
                    + lhs
                    + r"\s*(?:"
                    + operator
                    + r")\s*"
                    + rhs
                    + r"(?![A-Za-z0-9_])",
                    question,
                    re.I,
                ):
                    found.add((left, right, operation))
    return next(iter(found)) if len(found) == 1 else None
