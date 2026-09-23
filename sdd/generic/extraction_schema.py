"""User descriptions and deterministic conversion rules for text imports."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=63)
    description: str = Field(min_length=1, max_length=2000)
    type: Literal["text", "integer", "number", "date"] = "text"
    nullable: bool = False
    on_missing: Literal["hold", "null"] = "hold"
    decimal_separator: Literal[".", ","] = "."
    group_separator: Literal["", ".", ",", " "] = ","
    percent: Literal["reject", "points", "fraction"] = "reject"

    @model_validator(mode="after")
    def valid(self):
        if (
            not self.name.strip()
            or len(self.name.encode("utf-8")) > 63
            or self.name.startswith("_sdd")
            or "\x00" in self.name
        ):
            raise ValueError("Invalid extraction column name")
        if not self.description.strip():
            raise ValueError("Column descriptions must not be blank")
        if self.on_missing == "null" and not self.nullable:
            raise ValueError("Missing values can become NULL only in nullable columns")
        if self.decimal_separator == self.group_separator:
            raise ValueError("Decimal and grouping separators must differ")
        return self

    def catalog_definition(self):
        return self.model_dump(include={"name", "description", "type", "nullable"})


def columns_from(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 24:
        raise ValueError("Describe between 1 and 24 columns")
    columns = [ExtractionColumn.model_validate(v) for v in values]
    if len({c.name.casefold() for c in columns}) != len(columns):
        raise ValueError("Column names must be unique ignoring case")
    return columns
