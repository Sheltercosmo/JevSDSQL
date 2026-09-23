"""Bounded business-rule parsing and shared, typed formula dependencies."""

import ast
from dataclasses import dataclass
import re

from .stage_dag import operation as op, ref, value


def group(text, start):
    if start >= len(text) or text[start] != "{":
        raise ValueError("Expected a braced mathematical operand")
    depth = 1
    for index in range(start + 1, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            return text[start + 1 : index], index + 1
    raise ValueError("Unclosed mathematical operand")


def mathematical_text(source):
    """Translate a small mathematical notation grammar, never executable code."""
    text = source.replace("\\\\", "\\")
    text = re.split(r",\s*\\text\{\s*where\b", text, maxsplit=1)[0]
    text = text.replace("\\left", "").replace("\\right", "")
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("\\_", "_")
    text = text.replace("\\times", "*").replace("\\cdot", "*")
    text = text.replace("\\leq", "<=").replace("\\geq", ">=").replace("\\neq", "!=")
    text = text.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    text = text.replace("\\log_{10}", "log10").replace("\\exp", "exp")
    for command, arity in (("frac", 2), ("sqrt", 1), ("text", 1), ("mathrm", 1)):
        marker = "\\" + command + "{"
        while marker in text:
            start = text.index(marker)
            body, end = group(text, start + len(command) + 1)
            body = mathematical_text(body)
            if arity == 2:
                while end < len(text) and text[end].isspace():
                    end += 1
                other, end = group(text, end)
                replacement = f"(({body}) / ({mathematical_text(other)}))"
            elif command == "sqrt":
                replacement = f"sqrt({body})"
            else:
                replacement = body
            text = text[:start] + replacement + text[end:]
    text = text.replace("{", "(").replace("}", ")").replace("^", "**")
    if "\\" in text or len(text) > 8000:
        raise ValueError("Unsupported mathematical notation")
    return re.sub(r"\b([A-Za-z_]\w*) value\b", r"\1", text).strip()


def expression(source):
    text = mathematical_text(source)
    text = re.sub(r"\bAND\b", "and", text, flags=re.I)
    text = re.sub(r"\bOR\b", "or", text, flags=re.I)
    text = re.sub(r"(?<![<>=!])=(?!=)", "==", text)
    tree = ast.parse(text, mode="eval")
    if len(list(ast.walk(tree))) > 400:
        raise ValueError("Formula expression exceeds its node budget")
    return tree.body


def symbol_key(name):
    return re.sub(r"[^\w]|_", "", name.casefold())


def assignment_of(source):
    return re.match(r"^([\w\s{}\\-]+?)\s*=(?!=)\s*(.*)$", source, re.S)


def parse_definition(definition):
    """Return an expression or reject unsupported prose without guessing."""
    source = re.sub(r"^For (?:a given|each) .+?,\s*", "", definition.strip(), flags=re.I)
    assignment = assignment_of(source)
    if assignment:
        source = re.split(r",\s*(?:using|where)\b", assignment[2], maxsplit=1)[0]
    if "\\begin{cases}" in source:
        before, cases = source.split("\\begin{cases}", 1)
        body, after = cases.split("\\end{cases}", 1)
        entries = re.split(r"\\\\\s*", body)
        result = "None"
        for entry in reversed(entries):
            output, condition = entry.split("&", 1)
            condition = mathematical_text(condition).strip()
            output = mathematical_text(output)
            if condition.casefold() == "otherwise":
                result = output
            elif condition.casefold().startswith("if "):
                result = f"({output} if ({condition[3:]}) else ({result}))"
            else:
                raise ValueError("Unsupported piecewise condition")
        source = mathematical_text(before) + "(" + result + ")" + mathematical_text(after)
    elif not assignment:
        tiers = re.search(r"\bis:\s*(.+)$", source, re.S)
        if tiers:
            result = "None"
            for part in reversed(re.split(r",\s*|\n\s*-\s*", tiers[1].strip().lstrip("- "))):
                part = part.strip()
                fallback = re.fullmatch(r"(.+?)\s+otherwise", part, re.I)
                if fallback:
                    result = repr(fallback[1].strip().strip("\"'"))
                    continue
                label, condition = re.split(r"\s+if\s+", part, maxsplit=1, flags=re.I)
                label = label.strip().strip("\"'")
                result = f"({label!r} if ({mathematical_text(condition)}) else ({result}))"
            source = result
        else:
            match = re.search(r"\b(?:where|with|if)\s+(.+)", source)
            if not match:
                raise ValueError("Rule requires a structured expression or supported predicate")
            source = re.split(
                r",\s*(?:where|allowing|signaling|ensuring|capable)\b", match[1], maxsplit=1
            )[0]
            source = re.sub(r",\s*(?:and\s+)?", " and ", source).rstrip(".")
    return expression(source)


@dataclass
class Rule:
    id: str
    name: str
    definition: str
    dependencies: tuple[str, ...]
    kind: str = ""

    @property
    def symbol(self):
        source = re.sub(r"^For (?:a given|each) .+?,\s*", "", self.definition.strip(), flags=re.I)
        match = assignment_of(source)
        return match[1].strip() if match else self.name


def rules_from(records):
    if not isinstance(records, list) or len(records) > 256:
        raise ValueError("Business knowledge requires at most 256 rules")
    if sum(len(str(record)) for record in records) > 256000:
        raise ValueError("Business knowledge exceeds its 256,000-character budget")
    rules = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or "id" not in record
            or not isinstance(record.get("definition"), str)
        ):
            raise ValueError("Each rule requires an ID and a text definition")
        if not isinstance(record.get("name", record.get("knowledge", "")), str):
            raise ValueError("Rule names must be text")
        identity = str(record["id"])
        deps = record.get("dependencies", record.get("children_knowledge", []))
        rule = Rule(
            identity,
            record.get("name", record.get("knowledge", "")),
            record["definition"],
            tuple(map(str, deps if isinstance(deps, list) else [])),
            record.get("type", ""),
        )
        if identity in rules or not rule.name or len(rule.definition) > 12000:
            raise ValueError("Rule IDs must be unique and definitions bounded")
        rules[identity] = rule
    return rules


class RuleGraph:
    """Resolve formula references once, preserving shared operands and failures."""

    def __init__(self, rules, fields, columns, bindings=None):
        self.rules, self.fields, self.columns = rules, fields, columns
        self.bindings = bindings or {}
        self.symbols = {}
        for key, rule in rules.items():
            aliases = [rule.symbol, rule.name, rule.name.split("(")[0].strip()]
            aliases += re.findall(r"\(([A-Za-z_]\w*)\)", rule.name)
            for alias in aliases:
                normalized = symbol_key(alias)
                previous = self.symbols.get(normalized)
                if previous and rules[previous].definition != rule.definition:
                    self.symbols[normalized] = None
                elif normalized not in self.symbols:
                    self.symbols[normalized] = key
        self.terms, self.errors, self.visiting = {}, {}, set()
        self.layers = {}
        self.reductions = {}
        self.windows = {}
        self.reduction_table = None

    def build(self, identity):
        if identity in self.terms:
            return self.terms[identity]
        if identity in self.errors:
            raise ValueError(self.errors[identity])
        if identity in self.visiting:
            raise ValueError("Cyclic formula dependency")
        self.visiting.add(identity)
        try:
            from .rule_reductions import reductions

            rule = self.rules[identity]
            alias = re.match(r"^([A-Za-z_]\w*)\s*(?:\(|$)", rule.name)
            matches = [
                k
                for k, field in self.fields.items()
                if alias and field.name.casefold() == alias[1].casefold()
            ]
            if rule.kind == "value_illustration" and len(matches) == 1:
                term = ref(matches[0])
                self.columns["k" + identity] = term.type(self.columns)
                self.terms[identity], self.layers[identity] = term, 1
                return term
            definition = reductions(self.rules[identity].definition, self.register_reduction)
            term = self.convert(parse_definition(definition))
            self.columns["k" + identity] = term.type(self.columns)
            self.terms[identity] = term
            self.layers[identity] = 1 + max(
                (self.layers[key[1:]] for key in term.columns() if key.startswith("k")), default=0
            )
            return term
        except (ValueError, SyntaxError, KeyError, TypeError) as exc:
            self.errors[identity] = str(exc)
            raise ValueError(str(exc)) from exc
        finally:
            self.visiting.remove(identity)

    def convert(self, node):
        if isinstance(node, ast.Constant) and isinstance(
            node.value, (int, float, str, bool, type(None))
        ):
            return value(node.value)
        if isinstance(node, ast.Name):
            symbol = symbol_key(node.id)
            if symbol in self.reductions:
                return ref(symbol)
            if symbol in self.symbols:
                identity = self.symbols[symbol]
                if identity is None:
                    raise ValueError(f"Ambiguous business-rule alias: {node.id}")
                self.build(identity)
                return ref("k" + identity)
            matches = [
                key
                for key, field in self.fields.items()
                if (
                    symbol_key(field.name) == symbol
                    or (key in self.bindings and symbol_key(field.name.split(".")[-1]) == symbol)
                )
                and (
                    self.reduction_table is None
                    or field.table.casefold() == self.reduction_table.casefold()
                )
            ]
            if len(matches) != 1:
                raise ValueError(f"Operand {node.id} has {len(matches)} catalog matches")
            return self.bindings.get(matches[0], ref(matches[0]))
        if isinstance(node, ast.Attribute):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.insert(0, node.attr)
                node = node.value
            if not isinstance(node, ast.Name):
                raise ValueError("Qualified operands must refer to catalog fields")
            parts.insert(0, node.id)
            matches = [
                (key, 2)
                for key, field in self.fields.items()
                if len(parts) >= 2
                and [field.table.casefold(), field.name.casefold()]
                == [p.casefold() for p in parts[:2]]
            ]
            if not matches:
                matches = [
                    (key, 1)
                    for key, field in self.fields.items()
                    if field.name.casefold() == parts[0].casefold()
                ]
            if len(matches) != 1:
                raise ValueError("Qualified operand has no unambiguous catalog binding")
            key, consumed = matches[0]
            if consumed == len(parts):
                return ref(key)
            if self.columns[key].kind not in {"text", "json"}:
                raise ValueError("JSON path requires a JSON or text column")
            term = ref(key)
            for part in parts[consumed:]:
                term = op("json_text", term, value(part))
            return term
        if isinstance(node, ast.BinOp):
            kinds = {
                ast.Add: "add",
                ast.Sub: "sub",
                ast.Mult: "mul",
                ast.Div: "div",
                ast.Pow: "power",
            }
            if type(node.op) in kinds:
                return op(
                    kinds[type(node.op)],
                    self.numeric(self.convert(node.left)),
                    self.numeric(self.convert(node.right)),
                )
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return op("sub", value(0), self.convert(node.operand))
        if isinstance(node, ast.BoolOp):
            terms = [self.convert(item) for item in node.values]
            result = terms[0]
            for term in terms[1:]:
                result = op("and" if isinstance(node.op, ast.And) else "or", result, term)
            return result
        if isinstance(node, ast.Compare):
            kinds = {
                ast.Eq: "eq",
                ast.NotEq: "ne",
                ast.Lt: "lt",
                ast.LtE: "le",
                ast.Gt: "gt",
                ast.GtE: "ge",
            }
            operands = [node.left, *node.comparators]
            terms = [
                op(kinds[type(operator)], self.convert(operands[i]), self.convert(operands[i + 1]))
                for i, operator in enumerate(node.ops)
            ]
            result = terms[0]
            for term in terms[1:]:
                result = op("and", result, term)
            return result
        if isinstance(node, ast.IfExp):
            return op(
                "case", self.convert(node.test), self.convert(node.body), self.convert(node.orelse)
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            if (
                node.func.id == "json"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                return op("json_text", self.convert(node.args[0]), value(node.args[1].value))
            if node.func.id == "num" and len(node.args) == 1:
                return op("number", self.convert(node.args[0]))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"log10", "sqrt", "exp", "abs"}
            and len(node.args) == 1
            and not node.keywords
        ):
            return op(node.func.id, self.convert(node.args[0]))
        raise ValueError(
            "Unsupported expression; only bounded arithmetic and predicates are allowed"
        )

    def numeric(self, term):
        if term.op == "json_text":
            return op("number", term)
        if term.type(self.columns).kind in {"integer", "number"} or term.op != "column":
            return term
        field = self.fields.get(term.args[0])
        if field is None:
            return term
        for rule in self.rules.values():
            if rule.name.casefold() != (field.table + "." + field.name).casefold():
                continue
            pairs = re.findall(r"'([^']+)'\s*=\s*(-?\d+(?:\.\d+)?)", rule.definition)
            default = re.search(r"\bothers\s*=\s*(-?\d+(?:\.\d+)?)", rule.definition)
            if not pairs:
                continue
            result = value(float(default[1])) if default else value(None)
            for category, number in reversed(pairs):
                result = op("case", op("eq", term, value(category)), value(float(number)), result)
            return result
        return term

    def register_reduction(self, table, reducer, operand, bindings=(), iterator=None):
        from .stage_dag import Column

        if table not in {field.table for field in self.fields.values()}:
            raise ValueError("Aggregate table is not in the catalog")
        term = None
        if operand is not None:
            if iterator:
                names = {f.name for f in self.fields.values() if f.table == table}
                operand = re.sub(
                    r"\b([A-Za-z_]\w*)_" + re.escape(iterator) + r"\b",
                    lambda m: m[1] if m[0] not in names and m[1] in names else m[0],
                    operand,
                )
            operand = re.sub(r"([A-Za-z_]\w*)->>'([^']+)'::float", r"num(json(\1, '\2'))", operand)
            self.reduction_table = table
            try:
                term = self.convert(expression(operand))
            finally:
                self.reduction_table = None
            op(reducer, term).type(self.columns)
        signature = (table, reducer, term, tuple(bindings))
        for key, item in self.reductions.items():
            if item == signature:
                return key
        key = "a" + str(len(self.reductions))
        self.reductions[key] = signature
        self.columns[key] = Column("number", reducer + " over " + table)
        return key

    def bind(self, identity, term):
        self.columns["k" + identity] = term.type(self.columns)
        self.terms[identity] = term
        self.layers[identity] = 1 + max(
            (self.layers[k[1:]] for k in term.columns() if k.startswith("k")), default=0
        )
        self.errors.pop(identity, None)
        return term

    def register_window(self, kind, operand, partition=(), descending=False):
        from .stage_dag import Column

        if kind not in {"percent_rank", "quartile", "avg", "sum", "rank", "dense_rank"}:
            raise ValueError("Unsupported relational rule window")
        column = operand.type(self.columns)
        if column.kind not in {"number", "integer"} or column.unit == "identifier":
            raise ValueError("Population statistics require a numeric measure, not an identifier")
        for term in partition:
            term.type(self.columns)
        spec = (kind, operand, tuple(partition), descending)
        for key, existing in self.windows.items():
            if spec == existing:
                return ref(key)
        key = "w" + str(len(self.windows))
        self.windows[key] = spec
        self.columns[key] = Column("number", kind)
        return ref(key)

    def describe(self, selected):
        return {
            "selected": selected,
            "relational_windows": {
                key: {
                    "operator": spec[0],
                    "dependencies": sorted(
                        spec[1].columns() | set().union(*(item.columns() for item in spec[2]))
                    ),
                }
                for key, spec in self.windows.items()
            },
            "compiled": [
                {
                    "id": key,
                    "name": self.rules[key].name,
                    "dependencies": list(self.rules[key].dependencies),
                    "depth": self.layers[key],
                    "state": "VALUE",
                    "expression": term.sql().sql(dialect="postgres"),
                }
                for key, term in self.terms.items()
            ],
            "unresolved": [
                {"id": key, "state": "UNKNOWN", "reason": error}
                for key, error in self.errors.items()
            ],
            "not_evaluated": [
                key for key in self.rules if key not in self.terms and key not in self.errors
            ],
        }
