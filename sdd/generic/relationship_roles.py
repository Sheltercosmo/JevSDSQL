"""Resolve semantic relationship roles before deterministic path construction."""

from collections import defaultdict

from .jev import choice


def bind_roles(ask, tenant, request, links, fields):
    grouped = defaultdict(list)
    for link in links:
        grouped[link.source, link.target].append(link)
    alternatives, questions = {}, {}
    for pair, edges in grouped.items():
        bundles = defaultdict(list)
        for index, edge in enumerate(edges):
            bundles[edge.constraint_id or ("edge", index)].append(edge)
        if len(bundles) < 2:
            continue
        key = f"relationship_role_{len(alternatives)}"
        alternatives[key] = list(bundles.values())
        questions[key] = choice(
            "Which relationship ROLE connects these tables for the requested population? The same lookup table can describe different attributes. Preserve every column of a composite relationship. Choose the relevant role, not the first graph edge.",
            {
                str(i): " AND ".join(
                    f"{edge.source}.{edge.source_column} = {edge.target}.{edge.target_column}"
                    for edge in bundle
                )
                for i, bundle in enumerate(alternatives[key])
            },
        )
    if not questions:
        return links
    chosen = ask(
        tenant, {"request": request, "required_operands": [f.label for f in fields]}, questions
    )
    excluded = {edge for bundles in alternatives.values() for bundle in bundles for edge in bundle}
    selected = [edge for edge in links if edge not in excluded]
    for key, bundles in alternatives.items():
        selected.extend(bundles[int(chosen[key])])
    return selected
