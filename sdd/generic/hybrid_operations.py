"""Bounded SQL transformations selected by semantic obligations, not dataset names."""

import re

from sqlglot import exp, parse_one


def validate_steps(steps):
    by_name = {step.name: step for step in steps}
    if len(by_name) != len(steps) or not steps or len(steps) > 32:
        raise ValueError("Operation plans require 1–32 uniquely named steps")
    pending, done = set(by_name), set()
    while pending:
        ready = {name for name in pending if set(by_name[name].depends_on) <= done}
        if not ready:
            raise ValueError("Operation dependencies contain a cycle or an unknown input")
        pending -= ready
        done |= ready


def local_alternatives(packet, candidates, review):
    """Apply bounded AST proposals from concrete evidence; originals remain available."""
    alternatives = []
    checks = review["checks"]
    for candidate in candidates:
        if candidate["operation"] != "select":
            continue
        tree = parse_one(candidate["sql"], read="postgres")
        joins = [
            j
            for j in tree.find_all(exp.Join)
            if not j.side and j.kind in ("", "INNER") and j.args.get("on")
        ]
        changed = []
        for i, join in enumerate(joins):
            if checks.get(f"check_preserve_{candidate['id']}_j{i}", 0) >= 0.8:
                join.set("kind", None)
                join.set("side", "LEFT")
                changed.append(i)
        if changed:
            alternatives.append(
                {
                    **candidate,
                    "id": "r" + candidate["id"],
                    "sql": tree.sql(dialect="postgres"),
                    "label": "保留总体"
                    if re.search(r"[\u4e00-\u9fff]", packet["request"])
                    else "Preserve population",
                    "derived_from": candidate["id"],
                    "transformation": {"operator": "left_join", "joins": changed},
                }
            )
        if candidate["id"] != review["selected"]:
            continue
        output_ticket = any(t["kind"] == "output" for t in review.get("repair_tickets", []))
        rejected = []
        for i in range(len(candidate.get("projections", []))):
            needed = checks.get(f"check_{candidate['id']}_output{i}", 1)
            role = review.get("output_roles", {}).get(f"check_role_{candidate['id']}_output{i}", {})
            support = role.get("probabilities", {}).get("support", 0)
            if (output_ticket and needed <= 0.2) or (support >= 0.75 and needed < 0.5):
                rejected.append(i)
        revised = project_without(candidate["sql"], rejected)
        if revised:
            alternatives.append(
                {
                    **candidate,
                    "id": "p" + candidate["id"],
                    "sql": revised,
                    "label": "所需输出"
                    if re.search(r"[\u4e00-\u9fff]", packet["request"])
                    else "Requested outputs",
                    "derived_from": candidate["id"],
                    "transformation": {"operator": "project", "removed": rejected},
                }
            )
    return alternatives


def project_without(sql, rejected):
    if not rejected:
        return None
    tree = parse_one(sql, read="postgres")
    if not isinstance(tree, exp.Select) or tree.args.get("distinct"):
        return None
    remaining = [p for i, p in enumerate(tree.expressions) if i not in rejected]
    if not remaining or len(remaining) == len(tree.expressions):
        return None
    aliases = {p.alias: p.this for p in tree.expressions if isinstance(p, exp.Alias)}
    for clause in ("order", "group", "having"):
        node = tree.args.get(clause)
        if node is None:
            continue
        if any(isinstance(n, exp.Literal) and not n.is_string for n in node.walk()):
            return None
        for column in list(node.find_all(exp.Column)):
            if not column.table and column.name in aliases:
                column.replace(aliases[column.name].copy())
    removed = [p for i, p in enumerate(tree.expressions) if i in rejected]
    if (
        not tree.args.get("group")
        and any(p.find(exp.AggFunc) for p in removed)
        and not any(p.find(exp.AggFunc) for p in remaining)
    ):
        return None
    tree.set("expressions", remaining)
    return tree.sql(dialect="postgres")


def identity_alternatives(packet, candidates):
    """Expose primary-key output as an alternative to an ambiguous ID projection."""
    catalog = {table["name"]: table for table in packet["catalog"]}
    alternatives = []
    for candidate in candidates:
        if candidate["operation"] != "select":
            continue
        tree = parse_one(candidate["sql"], read="postgres")
        if not isinstance(tree, exp.Select):
            continue
        sources = {}
        source = tree.args.get("from_")
        tables = ([source.this] if source else []) + [j.this for j in tree.args.get("joins", [])]
        for table in tables:
            if isinstance(table, exp.Table) and table.name in catalog:
                sources[table.alias_or_name] = catalog[table.name]
        changed = []
        for projection in tree.expressions:
            column = projection.this if isinstance(projection, exp.Alias) else projection
            label = projection.alias_or_name.casefold()
            if not isinstance(column, exp.Column) or label not in ("id", "identifier", "key"):
                continue
            dataset = sources.get(column.table)
            if dataset is None and not column.table and len(sources) == 1:
                dataset = next(iter(sources.values()))
            if (
                dataset
                and len(dataset["primary_key"]) == 1
                and column.name != dataset["primary_key"][0]
            ):
                changed.append({"from": column.sql(), "to": dataset["primary_key"][0]})
                column.set("this", exp.to_identifier(dataset["primary_key"][0], quoted=True))
        if changed:
            alternatives.append(
                {
                    **candidate,
                    "id": "i" + candidate["id"][1:],
                    "label": "使用已登记的主键"
                    if re.search(r"[\u4e00-\u9fff]", packet["request"])
                    else "Registered primary-key identifiers",
                    "sql": tree.sql(dialect="postgres"),
                    "derived_from": candidate["id"],
                    "transformation": {"operator": "project_primary_key", "columns": changed},
                }
            )
    return alternatives
