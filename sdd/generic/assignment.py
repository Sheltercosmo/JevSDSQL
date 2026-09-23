"""Maximum-probability slot assignment with uniqueness and coverage constraints."""

import math


def assign_unique(distributions, *, required=(), fixed=None):
    fixed = fixed or {}
    columns = sorted({key for row in distributions for key in row})
    required = set(required)
    if len(columns) < len(distributions) or len(required) > len(distributions):
        raise ValueError("Output slots cannot cover the required distinct columns")
    if not required <= set(columns):
        raise ValueError("A required output is unavailable")
    count, width = len(distributions), len(columns)
    costs = []
    for index, row in enumerate(distributions):
        costs.append(
            [
                1e9
                if key not in row or (index in fixed and fixed[index] != key)
                else -math.log(max(row[key], 1e-15)) - (1000 if key in required else 0)
                for key in columns
            ]
        )
    # Rectangular Hungarian assignment; alternatives are matched together rather
    # than explored through a sequence of greedy output choices.
    u, v, owner, previous = (
        [0.0] * (count + 1),
        [0.0] * (width + 1),
        [0] * (width + 1),
        [0] * (width + 1),
    )
    for row_index in range(1, count + 1):
        owner[0] = row_index
        cursor, distance, used = 0, [float("inf")] * (width + 1), [False] * (width + 1)
        while True:
            used[cursor] = True
            active, delta, next_column = owner[cursor], float("inf"), 0
            for candidate in range(1, width + 1):
                if used[candidate]:
                    continue
                cost = costs[active - 1][candidate - 1] - u[active] - v[candidate]
                if cost < distance[candidate]:
                    distance[candidate], previous[candidate] = cost, cursor
                if distance[candidate] < delta:
                    delta, next_column = distance[candidate], candidate
            for candidate in range(width + 1):
                if used[candidate]:
                    u[owner[candidate]] += delta
                    v[candidate] -= delta
                else:
                    distance[candidate] -= delta
            cursor = next_column
            if owner[cursor] == 0:
                break
        while cursor:
            next_column = previous[cursor]
            owner[cursor] = owner[next_column]
            cursor = next_column
    selected = [None] * count
    for column_index in range(1, width + 1):
        if owner[column_index]:
            selected[owner[column_index] - 1] = columns[column_index - 1]
    if not required <= set(selected) or any(
        index in fixed and fixed[index] != key for index, key in enumerate(selected)
    ):
        raise ValueError("Output corrections conflict with uniqueness or coverage")
    if any(costs[i][columns.index(key)] >= 1e8 for i, key in enumerate(selected)):
        raise ValueError("No compatible output assignment")
    return selected
