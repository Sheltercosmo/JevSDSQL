"""Extract scoped reductions while retaining every declared relationship binding."""

import re

from .knowledge import group


def relationship_bindings(source):
    text = re.sub(r"\\text\{([^{}]+)\}", r"\1", source).strip().rstrip(".")
    pairs = []
    for clause in text.split(","):
        match = re.fullmatch(
            r"\s*([A-Za-z_]\w*(?:\.\w+)?)\s*=\s*([A-Za-z_]\w*(?:\.\w+)?)\s*", clause
        )
        if not match:
            raise ValueError("Unsupported aggregate-domain restriction; no predicate was discarded")
        pairs.append((match[1], match[2]))
    return tuple(pairs)


def domain_parts(domain):
    match = re.search(r"\\in\s*(?:\\text\{(\w+)\}|(\w+))", domain)
    if not match:
        raise ValueError("Aggregate domain must name one catalog table")
    bindings = relationship_bindings(domain.split(r"\mid", 1)[1]) if r"\mid" in domain else ()
    return match[1] or match[2], bindings


def table_in(domain):
    return domain_parts(domain)[0]


def reductions(source, register):
    local = re.search(
        r",\s*\\text\{\s*where\s*\}\s*([A-Za-z_]\w*)\(([A-Za-z_]\w*)\)\s*=\s*(.+)$", source, re.S
    )
    if local:
        name, parameter, body = local.groups()
        invocation = re.compile(
            r"\b" + re.escape(name) + r"\(\s*" + re.escape(parameter) + r"\s*\)"
        )
        if invocation.search(body):
            raise ValueError("Recursive local formula")
        source = invocation.sub(lambda _: "(" + body + ")", source[: local.start()])
    source = source.replace(r"\text{|}", "|").replace(r"\{", "{").replace(r"\}", "}")
    source = source.replace(r"\left", "").replace(r"\right", "")
    source = re.sub(r"\|\s*\{", "|{", source)
    while True:
        match = re.search(r"\\(sum|max|min)_\{", source)
        if not match:
            break
        domain, end = group(source, match.end() - 1)
        table, bindings = domain_parts(domain)
        start = end
        while start < len(source) and source[start].isspace():
            start += 1
        if start < len(source) and source[start] == "(":
            depth, end = 1, start + 1
            while end < len(source) and depth:
                depth += (source[end] == "(") - (source[end] == ")")
                end += 1
            if depth:
                raise ValueError("Unclosed aggregate operand")
            operand = source[start + 1 : end - 1]
        else:
            operand_match = re.match(r"(?:\\text\{\w+\}|\w+)", source[start:])
            if not operand_match:
                raise ValueError("Unsupported aggregate operand")
            operand = operand_match[0]
            end = start + len(operand)
        trailing = re.match(r",\s*(?:\\quad\s*)?\\text\{\s*where\s*\}\s*(.+)$", source[end:], re.S)
        if trailing:
            bindings += relationship_bindings(trailing[1])
            end = len(source)
        iterator = re.match(r"\s*(\w+)\s*\\in", domain)
        extra = (
            {"iterator": iterator[1]}
            if iterator and re.search(r"_" + re.escape(iterator[1]) + r"\b", operand)
            else {}
        )
        source = (
            source[: match.start()]
            + register(table, match[1], operand, bindings=bindings, **extra)
            + source[end:]
        )
    while "|{" in source:
        start = source.index("|{")
        domain, end = group(source, start + 1)
        closing = re.match(r"\s*\|", source[end:])
        if not closing:
            raise ValueError("Unclosed aggregate cardinality")
        table, bindings = domain_parts(domain)
        source = (
            source[:start]
            + register(table, "count", None, bindings=bindings)
            + source[end + len(closing[0]) :]
        )
    source = re.sub(r"\|([A-Za-z_]\w*)\|", lambda m: register(m[1], "count", None), source)
    return source
