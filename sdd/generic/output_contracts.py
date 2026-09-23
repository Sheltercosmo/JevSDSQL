"""Reconcile bounded output sets and order after parallel field grounding."""

from itertools import combinations, permutations

from .jev import choice


def output_tuple(
    ask, tenant, request, labels, proposed, *, prefix="answer", locked=False, candidates=()
):
    ordered = list(dict.fromkeys(proposed))
    if locked or len(ordered) > 4:
        return proposed
    pool = list(dict.fromkeys([*ordered, *candidates]))[:5]
    if not pool:
        return proposed
    if len(pool) > 1:
        sets = [
            candidate
            for length in range(1, min(4, len(pool)) + 1)
            for candidate in combinations(pool, length)
        ]
        result = ask(
            tenant,
            {"request": request},
            {
                prefix + "_set": choice(
                    "Which complete set of answer values does the user request? Include every component of a requested concept. "
                    "Exclude attributes used only for joining, filtering, ranking or calculating. "
                    "An entity's name does not implicitly request its identifier. Compare each complete set, not one preferred field.",
                    {
                        str(i): "Return " + " AND ".join(labels[k] for k in candidate)
                        for i, candidate in enumerate(sets)
                    },
                )
            },
            "output_coverage_reconciliation",
        )
        ordered = list(sets[int(result[prefix + "_set"])])
    if len(ordered) > 1:
        alternatives = list(permutations(ordered))
        result = ask(
            tenant,
            {"request": request},
            {
                prefix + "_tuple": choice(
                    "Choose the complete ordered answer tuple. Keep components of the same requested concept together in their natural order, followed by the next requested attribute or measure.",
                    {
                        str(i): " | ".join(labels[k] for k in candidate)
                        for i, candidate in enumerate(alternatives)
                    },
                )
            },
            "output_tuple_reconciliation",
        )
        ordered = list(alternatives[int(result[prefix + "_tuple"])])
    return ordered


def output_candidates(answers, prefix):
    scores = {}
    for key, answer in answers.items():
        if key.startswith(prefix):
            for field, probability in answer.get("probabilities", {}).items():
                if field != "none" and probability > 0:
                    scores[field] = max(probability, scores.get(field, 0))
    return sorted(scores, key=scores.get, reverse=True)
