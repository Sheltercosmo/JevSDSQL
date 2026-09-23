"""Shared relational expressions with independent aggregate and filter scopes."""

from copy import copy
from dataclasses import dataclass, field

from sqlglot import exp

from .relational import Program, compare, nonnull, ratio, statistic
from .sql import literal


def predicate(node, operator, value):
    if operator == "null":
        return exp.Is(this=node.copy(), expression=exp.Null())
    if operator == "not_null":
        return nonnull(node)
    if operator == "between":
        return exp.Between(this=node.copy(), low=literal(value[0]), high=literal(value[1]))
    if operator == "semantic":
        return exp.Anonymous(this="SEMANTIC", expressions=[node.copy(), literal(value)])
    return compare(node, operator, literal(value))


@dataclass
class AnalyticProgram(Program):
    metrics: dict = field(default_factory=dict)
    numerator_filters: list = field(default_factory=list)
    denominator_filters: list = field(default_factory=list)
    ratio_kind: str | None = None
    ratio_scale: int = 100
    offset: int = 0
    range_limit: int | None = None
    required_links: list = field(default_factory=list)

    def base(self):
        expanded = copy(self)
        used = set(self.outputs + [key for key, _ in self.order])
        operands = [operand for key, (_, operand) in self.metrics.items() if key in used]
        operands.extend(key for key, _, _ in self.numerator_filters + self.denominator_filters)
        expanded.outputs = list(dict.fromkeys([*self.outputs, *operands]))
        expanded.links = list(dict.fromkeys([*self.required_links, *self.links]))
        query, aliases, edges = Program.base(expanded)
        for link in self.required_links:
            if link not in edges and link.source in aliases and link.target in aliases:
                query = query.where(link.predicate(aliases))
                edges.append(link)
        return query, aliases, edges

    def expressions(self, aliases, period_aliases=None):
        values = super().expressions(aliases, period_aliases)
        used = set(self.outputs + [key for key, _ in self.order])
        for key, (kind, operand) in self.metrics.items():
            if operand in values and (not self.outputs or key in used):
                values[key] = statistic(kind, values[operand])
        if self.ratio_kind:
            measure = literal(1) if self.ratio_kind == "count" else values[self.measure]

            def aggregate(filters):
                value = measure.copy()
                if filters:
                    condition = exp.and_(
                        *[predicate(values[key], op, operand) for key, op, operand in filters]
                    )
                    value = exp.Case(ifs=[exp.If(this=condition, true=value)], default=literal(0))
                return exp.Sum(this=value)

            values["stat"] = exp.Mul(
                this=literal(self.ratio_scale),
                expression=ratio(
                    aggregate(self.numerator_filters), aggregate(self.denominator_filters)
                ),
            )
        return values

    def compile(self):
        query = super().compile()
        if self.range_limit is not None:
            query = query.limit(self.range_limit)
        return query.offset(self.offset) if self.offset else query
