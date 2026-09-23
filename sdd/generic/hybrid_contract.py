"""Compact model output and structural facts derived from SQL, not inferred intent."""

from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    depends_on: list[str]
    purpose: str
    operator: Literal[
        "scan",
        "join",
        "filter",
        "aggregate",
        "window",
        "project",
        "sort",
        "limit",
        "update",
        "insert",
        "delete",
        "set_operation",
    ]
    grain: str
    expressions: list[str]


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    sql: str = Field(min_length=1, max_length=30000)
    assumptions: list[str]


class DataConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    columns: list[str]
    row_scope: str
    grain: str
    keys_and_relationships: str
    units_and_nulls: str
    purpose: str


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    result_grain: str
    outputs: list[str]
    steps: list[PlanStep]
    data_constraints: list[DataConstraint] = Field(max_length=32)
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    preferred_index: int = Field(ge=0, le=2)


class CompactDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    preferred_index: int = Field(ge=0, le=2)


def _sql_facts(sql, packet):
    """Describe implemented structure; the request remains the authority for meaning."""
    tree = parse_one(sql, read="postgres")
    catalog = {t["name"].casefold(): t for t in packet["catalog"]}
    chinese = bool(re.search(r"[\u4e00-\u9fff]", packet["request"]))
    labels = {
        "set_operation": ("Combine query results", "合并查询结果"),
        "scan": ("Read source", "读取来源"),
        "join": ("Join inputs", "关联输入"),
        "filter": ("Apply predicate", "应用条件"),
        "aggregate": ("Aggregate rows", "汇总记录"),
        "window": ("Compute windows", "计算窗口"),
        "project": ("Return expressions", "返回表达式"),
        "sort": ("Order rows", "排序"),
        "limit": ("Limit rows", "限制记录数"),
        "update": ("Update records", "更新记录"),
        "insert": ("Insert records", "插入记录"),
        "delete": ("Delete records", "删除记录"),
    }
    steps, constraints, sources, scope_outputs = [], {}, {}, {}
    source_grain = "来源记录" if chinese else "source records"
    unknown_grain = (
        "实际记录粒度；业务粒度待核验"
        if chinese
        else "implemented row grain; intended business grain requires review"
    )

    def add(operator, inputs, expressions, grain):
        name = "s" + str(len(steps))
        steps.append(
            PlanStep(
                name=name,
                depends_on=list(dict.fromkeys(inputs)),
                operator=operator,
                purpose=labels[operator][int(chinese)],
                grain=grain,
                expressions=expressions,
            )
        )
        return name

    scopes = traverse_scope(tree) or []
    for scope in scopes:
        node = scope.expression
        inputs = []
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, Scope):
                if id(source) in scope_outputs:
                    inputs.append(scope_outputs[id(source)])
                continue
            if not isinstance(source, exp.Table):
                continue
            table = catalog.get(source.name.casefold(), {})
            if source.name not in sources:
                sources[source.name] = add("scan", [], [source.name], source_grain)
            inputs.append(sources[source.name])
            data = constraints.setdefault(
                source.name,
                {
                    "source": source.name,
                    "columns": [],
                    "predicates": [],
                    "grain": ("主键：" if chinese else "keys: ")
                    + ", ".join(table.get("primary_key", []))
                    if table.get("primary_key")
                    else (
                        "来源记录；唯一性未确定"
                        if chinese
                        else "source records; uniqueness not established"
                    ),
                    "keys_and_relationships": str(
                        {
                            "keys": table.get("primary_key", []),
                            "relationships": table.get("relationships", []),
                        }
                    ),
                    "units_and_nulls": "转换由 SQL 表达式和列类型决定；业务单位需明确规则。"
                    if chinese
                    else "SQL expressions and registered column types define implemented conversions; business units require explicit rules.",
                    "purpose": "SQL source" if not chinese else "SQL 数据来源",
                },
            )
            data["columns"].extend(c.name for c in scope.columns if c.table in ("", alias))
            data["predicates"].extend(
                node.args[k].sql(dialect="postgres")
                for k in ("where", "having")
                if node.args.get(k)
            )
        inputs.extend(
            scope_outputs[id(child)]
            for child in [*scope.subquery_scopes, *scope.union_scopes]
            if id(child) in scope_outputs
        )
        grain = unknown_grain
        if isinstance(node, exp.SetOperation):
            inputs = [
                add(
                    "set_operation",
                    inputs,
                    [
                        node.key.upper()
                        + (" ALL" if node.args.get("distinct") is False else " DISTINCT")
                    ],
                    grain,
                )
            ]
        elif node.args.get("joins"):
            inputs = [
                add(
                    "join",
                    inputs,
                    [j.sql(dialect="postgres") for j in node.args.get("joins", [])],
                    grain,
                )
            ]
        for clause in ("where",):
            if node.args.get(clause):
                inputs = [add("filter", inputs, [node.args[clause].sql(dialect="postgres")], grain)]
        aggregates = [
            a.sql(dialect="postgres")
            for a in scope.find_all(exp.AggFunc)
            if not a.find_ancestor(exp.Window)
        ]
        if node.args.get("group") or aggregates:
            grain = (
                node.args["group"].sql(dialect="postgres")
                if node.args.get("group")
                else ("单行汇总" if chinese else "one aggregate row")
            )
            inputs = [
                add(
                    "aggregate",
                    inputs,
                    ([grain] if node.args.get("group") else []) + aggregates,
                    grain,
                )
            ]
        if node.args.get("having"):
            inputs = [add("filter", inputs, [node.args["having"].sql(dialect="postgres")], grain)]
        windows = [w.sql(dialect="postgres") for w in scope.find_all(exp.Window)]
        if windows:
            inputs = [add("window", inputs, windows, grain)]
        expressions = [p.sql(dialect="postgres") for p in node.selects]
        inputs = [add("project", inputs, expressions, grain)]
        for clause, operator in (("order", "sort"), ("limit", "limit")):
            if node.args.get(clause):
                inputs = [add(operator, inputs, [node.args[clause].sql(dialect="postgres")], grain)]
        scope_outputs[id(scope)] = inputs[-1]
    if not steps:
        operator = tree.key if tree.key in ("update", "insert", "delete") else "project"
        add(operator, [], [tree.sql(dialect="postgres")], "affected records")
        for source in tree.find_all(exp.Table):
            constraints[source.name] = {
                "source": source.name,
                "columns": [c.name for c in tree.find_all(exp.Column)],
                "predicates": [tree.args["where"].sql(dialect="postgres")]
                if tree.args.get("where")
                else [],
                "grain": "affected records",
                "keys_and_relationships": "registered target keys",
                "units_and_nulls": "SQL expressions",
                "purpose": "SQL target" if not chinese else "SQL 目标",
            }
    data = []
    for item in constraints.values():
        predicates = item.pop("predicates")
        item["columns"] = list(dict.fromkeys(item["columns"]))
        item["row_scope"] = "; ".join(dict.fromkeys(predicates)) or (
            "No explicit predicate" if not chinese else "无显式条件"
        )
        data.append(DataConstraint(**item))
    return {
        "steps": steps,
        "data_constraints": data,
        "outputs": [p.sql(dialect="postgres") for p in getattr(tree, "selects", [])],
        "result_grain": steps[-1].grain,
        "fact_source": "sql_ast",
        "intent_verified": False,
        "output_state": "VALUE",
    }


def sql_facts(sql, packet):
    try:
        return _sql_facts(sql, packet)
    except (SqlglotError, ValueError, AttributeError, KeyError, TypeError):
        chinese = bool(re.search(r"[\u4e00-\u9fff]", packet["request"]))
        return {
            "steps": [
                PlanStep(
                    name="unresolved",
                    depends_on=[],
                    purpose="结构尚未解析" if chinese else "Structure not resolved",
                    operator="project",
                    grain="UNKNOWN",
                    expressions=[],
                )
            ],
            "outputs": [],
            "data_constraints": [],
            "result_grain": "UNKNOWN",
            "fact_source": "sql_ast",
            "intent_verified": False,
            "output_state": "UNKNOWN",
        }


def materialize(value, packet):
    if "steps" in value:
        return Draft.model_validate(value)
    compact = CompactDraft.model_validate(value)
    if compact.preferred_index >= len(compact.candidates):
        raise ValueError("Invalid preferred candidate")
    facts = sql_facts(compact.candidates[compact.preferred_index].sql, packet)
    return Draft(
        objective=packet["request"],
        **{k: facts[k] for k in ("steps", "outputs", "data_constraints", "result_grain")},
        **compact.model_dump(),
    )
