"""Typed relational programs compiled independently of linguistic decisions."""

from dataclasses import dataclass, field
from collections import deque

from sqlglot import exp

from .sql import col, literal


def table(name, alias):
    return exp.Table(
        this=exp.to_identifier(name, quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)),
    )


def compare(left, operator, right):
    return {"eq": exp.EQ, "ne": exp.NEQ, "lt": exp.LT, "le": exp.LTE, "gt": exp.GT, "ge": exp.GTE}[
        operator
    ](this=left.copy(), expression=right.copy())


def nonnull(node):
    return exp.Not(this=exp.Is(this=node.copy(), expression=exp.Null()))


def ratio(numerator, denominator):
    return exp.Div(
        typed=True,
        this=exp.Paren(
            this=exp.Mul(this=literal(1.0), expression=exp.Paren(this=numerator.copy()))
        ),
        expression=exp.Nullif(this=denominator.copy(), expression=literal(0)),
    )


def statistic(kind, node=None, weight=None):
    if kind == "count":
        return exp.Count(this=exp.Star())
    if node is None:
        raise ValueError("A statistic requires a measure")
    if kind == "value":
        return node.copy()
    if kind == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[node.copy()]))
    if kind == "weighted":
        if weight is None:
            raise ValueError("A weighted mean requires a weight")
        return ratio(
            exp.Sum(this=exp.Mul(this=node.copy(), expression=weight.copy())),
            exp.Sum(this=weight.copy()),
        )
    return {"sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[kind](this=node.copy())


@dataclass(frozen=True)
class Field:
    key: str
    table: str
    name: str
    kind: str
    description: str = ""
    feature_id: str | None = None
    source_column: str | None = None

    @property
    def label(self):
        return f"{self.table}.{self.name} ({self.kind}) {self.description}"

    def expression(self, aliases):
        alias = aliases[self.table]
        if self.feature_id:
            return exp.Anonymous(
                this="SEMANTIC_FEATURE",
                expressions=[col(self.source_column, alias), literal(self.feature_id)],
            )
        return col(self.name, alias)


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    source_column: str
    target_column: str
    inferred: bool = False
    cast_source: bool = False
    cast_target: bool = False
    constraint_id: str | None = None

    def predicate(self, aliases):
        left, right = (
            col(self.source_column, aliases[self.source]),
            col(self.target_column, aliases[self.target]),
        )
        if self.cast_source:
            left = exp.Cast(this=left, to=exp.DataType.build("TEXT"))
        if self.cast_target:
            right = exp.Cast(this=right, to=exp.DataType.build("TEXT"))
        return exp.EQ(this=left, expression=right)


def connect(root, needed, links):
    """Find a minimal connecting tree by shortest approved/proposed paths."""
    joined, chosen = {root}, []
    for target in sorted(set(needed) - joined):
        queue = deque((start, []) for start in sorted(joined))
        visited = set(joined)
        found = None
        while queue:
            node, path = queue.popleft()
            if node == target:
                found = path
                break
            for edge in sorted(links, key=lambda item: item.inferred):
                if node not in (edge.source, edge.target):
                    continue
                other = edge.target if node == edge.source else edge.source
                if other not in visited:
                    visited.add(other)
                    queue.append((other, [*path, edge]))
        if found is None:
            raise ValueError(f"No relationship path connects {root} to {target}")
        expanded = []
        for edge in found:
            expanded.extend(
                [
                    other
                    for other in links
                    if edge.constraint_id
                    and other.constraint_id == edge.constraint_id
                    and (other.source, other.target) == (edge.source, edge.target)
                ]
                or [edge]
            )
        for edge in expanded:
            if edge not in chosen:
                chosen.append(edge)
            joined.update((edge.source, edge.target))
    return chosen


@dataclass
class Program:
    root: str
    fields: dict[str, Field]
    links: list[Link]
    family: str = "rows"
    measure: str | None = None
    weight: str | None = None
    entity: str | None = None
    time: str | None = None
    periods: list = field(default_factory=list)
    partition: str | None = None
    aggregation: str = "value"
    formula: str = "identity"
    filters: list = field(default_factory=list)
    temporal_conditions: list = field(default_factory=list)
    baseline_operator: str = "lt"
    trend_direction: str = "gt"
    outputs: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    order: list[tuple[str, bool]] = field(default_factory=list)
    limit: int | None = None
    distinct: bool = False
    rank_descending: bool = True
    absent_table: str | None = None

    def base(self):
        required = set(self.outputs + self.groups + [key for key, _ in self.order])
        roles = [self.measure]
        if self.formula != "identity" or self.aggregation == "weighted" or self.family == "periods":
            roles.append(self.weight)
        if self.family in ("periods", "trend"):
            roles.extend((self.entity, self.time))
        if self.family in ("baseline", "partition_rank", "groups", "periods"):
            roles.append(self.partition)
        required.update(key for key in roles if key)
        required.update(item[0] for item in self.filters)
        needed = {self.fields[key].table for key in required if key in self.fields}
        edges = connect(self.root, needed, self.links)
        aliases = {self.root: "r0"}
        query = exp.select().from_(table(self.root, aliases[self.root]))
        for edge in edges:
            other = edge.target if edge.source in aliases else edge.source
            if other in aliases:
                predicate = edge.predicate(aliases)
                joined = next(
                    (
                        join
                        for join in reversed(query.args.get("joins", []))
                        if join.this.alias_or_name in (aliases[edge.source], aliases[edge.target])
                    ),
                    None,
                )
                if joined is not None:
                    joined.set("on", exp.and_(joined.args["on"], predicate))
                else:
                    query = query.where(predicate)
                continue
            aliases[other] = "r" + str(len(aliases))
            query = query.join(
                table(other, aliases[other]),
                on=edge.predicate(aliases),
                join_type="LEFT" if self.family == "absence" else "INNER",
            )
        return query, aliases, edges

    def expressions(self, aliases, period_aliases=None):
        values = {
            key: field.expression(aliases)
            for key, field in self.fields.items()
            if field.table in aliases
        }
        measure = values.get(self.measure)
        weight = values.get(self.weight)
        expression = measure
        if self.formula != "identity":
            if measure is None or weight is None:
                raise ValueError("Arithmetic needs two numeric operands")
            expression = (
                ratio(measure, weight)
                if self.formula == "ratio"
                else {"product": exp.Mul, "sum": exp.Add, "difference": exp.Sub}[self.formula](
                    this=measure.copy(), expression=weight.copy()
                )
            )
            values["calculation"] = expression
        if self.aggregation != "value":
            values["stat"] = statistic(self.aggregation, expression, weight)
        if self.family == "baseline" and measure is not None:
            partitions = [values[self.partition].copy()] if self.partition else []
            baseline = exp.Window(this=statistic("avg", measure), partition_by=partitions)
            values["baseline"] = baseline
            values["shortfall"] = exp.Sub(this=baseline.copy(), expression=measure.copy())
            values["surplus"] = exp.Sub(this=measure.copy(), expression=baseline.copy())
        if period_aliases:
            for key in (self.measure, self.weight):
                if not key:
                    continue
                for index, alias in enumerate(period_aliases):
                    values[f"{key}@{index}"] = self.fields[key].expression(
                        {**aliases, self.root: alias}
                    )
            for a in range(len(period_aliases)):
                for b in range(a + 1, len(period_aliases)):
                    for key in (self.measure, self.weight):
                        if not key:
                            continue
                        before, after = values[f"{key}@{a}"], values[f"{key}@{b}"]
                        gain = exp.Sub(this=after.copy(), expression=before.copy())
                        values[f"gain:{key}:{a}:{b}"] = gain
                        values[f"loss:{key}:{a}:{b}"] = exp.Sub(
                            this=before.copy(), expression=after.copy()
                        )
                        values[f"percent:{key}:{a}:{b}"] = exp.Mul(
                            this=literal(100.0), expression=ratio(gain, before)
                        )
                    if self.aggregation == "weighted" and self.weight:
                        first = statistic(
                            "weighted", values[f"{self.measure}@{a}"], values[f"{self.weight}@{a}"]
                        )
                        last = statistic(
                            "weighted", values[f"{self.measure}@{b}"], values[f"{self.weight}@{b}"]
                        )
                        values[f"weighted_gain:{a}:{b}"] = exp.Sub(this=last, expression=first)
        return values

    def compile(self):
        query, aliases, edges = self.base()
        predicates = []
        period_aliases = []
        if self.family == "periods":
            if not self.entity or not self.time or len(self.periods) < 2:
                raise ValueError(
                    "A temporal comparison needs entity, time and at least two periods"
                )
            if any(
                self.fields[key].table != self.root
                for key in (self.entity, self.time, self.measure)
                if key
            ):
                raise ValueError("Temporal roles must belong to the same observation relation")
            period_aliases = [aliases[self.root]]
            predicates.append(
                compare(
                    col(self.fields[self.time].name, period_aliases[0]),
                    "eq",
                    literal(self.periods[0]),
                )
            )
            for index, period in enumerate(self.periods[1:], 1):
                alias = f"p{index}"
                period_aliases.append(alias)
                predicate = exp.and_(
                    compare(
                        col(self.fields[self.entity].name, alias),
                        "eq",
                        col(self.fields[self.entity].name, period_aliases[0]),
                    ),
                    compare(col(self.fields[self.time].name, alias), "eq", literal(period)),
                )
                query = query.join(table(self.root, alias), on=predicate, join_type="INNER")
        values = self.expressions(aliases, period_aliases)
        if self.family == "baseline" and not self.partition:
            if any(
                operator == "semantic" or self.fields[key].table != self.root
                for key, operator, _ in self.filters
            ):
                raise ValueError(
                    "Global baseline scope with semantic or related-table filters requires an explicit SQL population"
                )
            source_alias = aliases[self.root]
            baseline_query = exp.select(statistic("avg", values[self.measure])).from_(
                table(self.root, source_alias)
            )
            root_conditions = []
            for key, operator, operand in self.filters:
                if self.fields[key].table != self.root:
                    continue
                node = values[key]
                if operator in ("eq", "ne", "lt", "le", "gt", "ge"):
                    root_conditions.append(compare(node, operator, literal(operand)))
                elif operator == "between":
                    root_conditions.append(
                        exp.Between(
                            this=node.copy(), low=literal(operand[0]), high=literal(operand[1])
                        )
                    )
                elif operator == "not_null":
                    root_conditions.append(nonnull(node))
                elif operator == "null":
                    root_conditions.append(exp.Is(this=node.copy(), expression=exp.Null()))
            if root_conditions:
                baseline_query = baseline_query.where(exp.and_(*root_conditions))
            baseline = exp.Subquery(this=baseline_query)
            values["baseline"] = baseline
            values["shortfall"] = exp.Sub(
                this=baseline.copy(), expression=values[self.measure].copy()
            )
            values["surplus"] = exp.Sub(
                this=values[self.measure].copy(), expression=baseline.copy()
            )
        for key, operator, operand in self.filters:
            node = values[key]
            if self.family == "periods" and key == self.time:
                continue
            if operator == "null":
                predicate = exp.Is(this=node.copy(), expression=exp.Null())
            elif operator == "not_null":
                predicate = nonnull(node)
            elif operator == "semantic":
                predicate = exp.Anonymous(
                    this="SEMANTIC", expressions=[node.copy(), literal(operand)]
                )
            elif operator == "between":
                predicate = exp.Between(
                    this=node.copy(), low=literal(operand[0]), high=literal(operand[1])
                )
            else:
                predicate = compare(node, operator, literal(operand))
            predicates.append(predicate)
        if self.family == "periods":
            for key in (self.measure, self.weight):
                if key:
                    predicates.extend(
                        nonnull(values[f"{key}@{i}"]) for i in range(len(period_aliases))
                    )
            for key, before, after, operator in self.temporal_conditions:
                predicates.append(
                    compare(values[f"{key}@{after}"], operator, values[f"{key}@{before}"])
                )
        if self.family == "absence":
            path = connect(self.root, [self.absent_table], self.links)
            anti_aliases = {self.root: aliases[self.root]}
            anti = None
            for edge in path:
                if edge.source in anti_aliases and edge.target in anti_aliases:
                    anti = anti.where(edge.predicate(anti_aliases))
                    continue
                other = edge.target if edge.source in anti_aliases else edge.source
                anti_aliases[other] = "absent_" + str(len(anti_aliases))
                source = table(other, anti_aliases[other])
                condition = edge.predicate(anti_aliases)
                anti = (
                    exp.select(literal(1)).from_(source).where(condition)
                    if anti is None
                    else anti.join(source, on=condition, join_type="INNER")
                )
            if anti is None:
                raise ValueError("Absence needs a related population")
            predicates.append(exp.Not(this=exp.Exists(this=anti)))
        if self.aggregation == "weighted":
            predicates.extend(nonnull(values[key]) for key in (self.measure, self.weight))
        if predicates:
            query = query.where(exp.and_(*predicates))
        if self.family == "trend":
            if not all((self.time, self.entity, self.measure)) or len(self.periods) != 2:
                raise ValueError("A complete trend requires entity, measure and interval")
            entity, moment, measure = (
                values[key] for key in (self.entity, self.time, self.measure)
            )
            previous = exp.Window(
                this=exp.Lag(this=measure.copy()),
                partition_by=[entity.copy()],
                order=exp.Order(expressions=[exp.Ordered(this=moment.copy())]),
            )
            query = query.where(
                exp.Between(
                    this=moment.copy(), low=literal(self.periods[0]), high=literal(self.periods[1])
                )
            )
            projected = [
                exp.alias_(node.copy(), key, quoted=True)
                for key, node in values.items()
                if key in self.fields
            ]
            query = query.select(
                *projected, exp.alias_(previous, "previous", quoted=True)
            ).subquery("steps")
            values = {key: col(key, "steps") for key in self.fields if key in values}
            transition = compare(
                values[self.measure], self.trend_direction, col("previous", "steps")
            )
            first = compare(values[self.time], "eq", literal(self.periods[0]))
            valid = exp.And(
                this=nonnull(values[self.measure]),
                expression=exp.Or(this=first, expression=transition),
            )
            count = int(self.periods[1]) - int(self.periods[0]) + 1
            query = (
                exp.select()
                .from_(query)
                .having(
                    exp.and_(
                        compare(exp.Count(this=exp.Star()), "eq", literal(count)),
                        compare(
                            exp.Sum(
                                this=exp.Case(
                                    ifs=[exp.If(this=valid, true=literal(1))], default=literal(0)
                                )
                            ),
                            "eq",
                            literal(count),
                        ),
                    )
                )
            )
            self_groups = list(dict.fromkeys([self.entity, *self.outputs]))
        else:
            self_groups = self.groups
        if self.family == "baseline":
            inner = query.select(
                *[exp.alias_(node.copy(), key, quoted=True) for key, node in values.items()]
            )
            query = exp.select().from_(inner.subquery("scoped"))
            values = {key: col(key, "scoped") for key in values}
            query = query.where(
                compare(values[self.measure], self.baseline_operator, values["baseline"])
            )
        if not self.outputs:
            raise ValueError("The output contract is empty")
        if any(key not in values for key in self.outputs):
            raise ValueError("The output contract references an unavailable expression")
        selected = [
            exp.alias_(values[key].copy(), f"result_{index + 1}", quoted=True)
            for index, key in enumerate(self.outputs)
        ]
        query = query.select(*selected)
        if self_groups:
            query = query.group_by(*[values[key].copy() for key in self_groups])
        if self.distinct:
            query = query.distinct()
        if self.family == "partition_rank":
            if not self.partition or not self.measure:
                raise ValueError("Partition ranking requires partition and ranking measure")
            rank_order = [
                (self.measure, self.rank_descending),
                *[
                    (key, direction)
                    for key, direction in self.order
                    if key not in (self.measure, self.partition)
                ],
            ]
            keys = list(
                dict.fromkeys(
                    [*self.outputs, self.partition, self.measure, *[key for key, _ in self.order]]
                )
            )
            query.set(
                "expressions", [exp.alias_(values[key].copy(), key, quoted=True) for key in keys]
            )
            ranking = exp.Window(
                this=exp.RowNumber(),
                partition_by=[values[self.partition].copy()],
                order=exp.Order(
                    expressions=[
                        exp.Ordered(this=values[key].copy(), desc=descending, nulls_first=False)
                        for key, descending in rank_order
                    ]
                ),
            )
            query = query.select(exp.alias_(ranking, "position", quoted=True)).subquery("ranked")
            query = (
                exp.select(
                    *[
                        exp.alias_(col(key, "ranked"), f"result_{index + 1}", quoted=True)
                        for index, key in enumerate(self.outputs)
                    ]
                )
                .from_(query)
                .where(compare(col("position", "ranked"), "le", literal(self.limit or 1)))
            )
            values = {key: col(key, "ranked") for key in keys}
            ordering = self.order or [(self.partition, False), *rank_order]
        else:
            ordering = self.order
            if self.limit is not None:
                query = query.limit(self.limit)
        if ordering:
            query = query.order_by(
                *[
                    exp.Ordered(this=values[key].copy(), desc=descending, nulls_first=False)
                    for key, descending in ordering
                ]
            )
        return query
