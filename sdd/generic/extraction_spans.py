"""Source offsets, bounded passages and exact scalar conversion."""

from datetime import date
from decimal import Decimal, DecimalException, localcontext
import re
import unicodedata

TOKEN = re.compile(
    r"[+-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?|[A-Za-zÀ-ÖØ-öø-ÿ_]+(?:[-'/][A-Za-zÀ-ÖØ-öø-ÿ_]+)*|[^\s]"
)
BOUNDARY = re.compile(r"\n+|(?<=[。！？!?；;])|(?<=\.)(?=\s+[A-Z0-9\"“])")


def passages(source, mode="auto", token_limit=160):
    if mode not in {"auto", "paragraph", "line", "document"}:
        raise ValueError("Record mode must be auto, paragraph, line or document")
    separator = (
        re.compile(r"\n\s*\n")
        if mode == "paragraph"
        else re.compile(r"\n+")
        if mode == "line"
        else BOUNDARY
    )
    ranges, start = [], 0
    for match in separator.finditer(source):
        ranges.append((start, match.start()))
        start = match.end()
    ranges.append((start, len(source)))
    output = []
    for group, (start, end) in enumerate(ranges):
        tokens = list(TOKEN.finditer(source, start, end))
        for i in range(0, len(tokens), token_limit):
            chunk = tokens[i : i + token_limit]
            output.append(
                {
                    "start": chunk[0].start(),
                    "end": chunk[-1].end(),
                    "group": group,
                    "split": len(tokens) > token_limit,
                }
            )
    return output


def token_spans(source, passage):
    return [
        {"start": m.start(), "end": m.end(), "text": m.group()}
        for m in TOKEN.finditer(source, passage["start"], passage["end"])
    ]


DATE_LITERAL = re.compile(r"\d{4}(?:-\d{2}-\d{2}|年\d{1,2}月\d{1,2}日)")
NUMBER_LITERAL = re.compile(
    r"(?:\(\s*)?(?:[$€£¥￥]\s*)?[+\-−－＋]?(?:\d+(?:[.,，．]\d+)*|\.\d+)"
    r"(?:[eE][+\-]?\d+)?(?:[ \u00a0]\d{3})*"
    r"(?:\s*(?:thousand|million|billion|万|亿|[%％]))?(?:\s*\))?",
    re.I,
)


def scalar_spans(source, passage, column):
    pattern = DATE_LITERAL if column.type == "date" else NUMBER_LITERAL
    output = []
    for match in pattern.finditer(source, passage["start"], passage["end"]):
        raw = match.group()
        try:
            value = convert(raw, column)
        except ValueError:
            continue
        output.append({"start": match.start(), "end": match.end(), "text": raw, "value": value})
    return output


def convert(raw, column):
    if column.type == "text":
        return raw
    value = unicodedata.normalize("NFKC", raw).replace("−", "-").strip()
    if len(value) > 200:
        raise ValueError("Numeric and date literals must be at most 200 characters")
    if column.type == "date":
        value = re.sub(
            r"^(\d{4})年(\d{1,2})月(\d{1,2})日$",
            lambda m: f"{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}",
            value,
        )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Dates require ISO YYYY-MM-DD or an explicit Chinese calendar date")
        return date.fromisoformat(value).isoformat()
    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1].strip()
    value = re.sub(r"^[\$€£¥]\s*", "", value)
    scale = Decimal(1)
    suffix = re.search(r"\s*(thousand|million|billion|万|亿)$", value, re.I)
    if suffix:
        scale = Decimal(
            {
                "thousand": 1000,
                "million": 1000000,
                "billion": 1000000000,
                "万": 10000,
                "亿": 100000000,
            }[suffix[1].lower()]
        )
        value = value[: suffix.start()].strip()
    if value.endswith("%"):
        if column.percent == "reject":
            raise ValueError("Specify whether a percentage uses points or a fraction")
        scale *= Decimal("0.01") if column.percent == "fraction" else 1
        value = value[:-1].strip()
    exponent = ""
    if match := re.search(r"[eE][+-]?\d+$", value):
        exponent, value = value[match.start() :], value[: match.start()]
        if abs(int(exponent[1:])) > 200:
            raise ValueError("Numeric exponent exceeds supported precision")
    decimal, group = column.decimal_separator, column.group_separator
    parts = value.split(decimal)
    if len(parts) > 2 or len(parts) == 2 and not parts[1].isdigit():
        raise ValueError("Invalid decimal notation")
    whole = parts[0]
    if group and group in whole:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:" + re.escape(group) + r"\d{3})+", whole):
            raise ValueError("Ambiguous or invalid digit grouping")
        whole = whole.replace(group, "")
    numeric = whole + ("." + parts[1] if len(parts) == 2 else "") + exponent
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?", numeric):
        raise ValueError("The selected span is not a supported numeric literal")
    try:
        with localcontext() as context:
            context.prec = 220
            context.Emax = 1000
            context.Emin = -1000
            number = Decimal(numeric) * scale * (-1 if negative else 1)
    except DecimalException as exc:
        raise ValueError("Invalid number") from exc
    if not number.is_finite():
        raise ValueError("Numbers must be finite")
    if column.type == "integer":
        if number != number.to_integral_value() or not -(2**63) <= number < 2**63:
            raise ValueError("Value does not fit a 64-bit integer")
        return int(number)
    digits = number.as_tuple().digits
    trailing_zeros = len(digits) - len("".join(map(str, digits)).rstrip("0"))
    decimal_places = max(0, -number.as_tuple().exponent - trailing_zeros)
    if decimal_places > 10 or number.copy_abs() >= Decimal(10) ** 28:
        raise ValueError("Value exceeds NUMERIC(38,10); use a text column to preserve it")
    return format(number, "f")
