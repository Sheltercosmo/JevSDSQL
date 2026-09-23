"""Description-driven text import: preview, optional immediate insert, replay-safe commit."""

from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field

from ..ir import Strict
from .extraction_schema import ExtractionColumn
from .text_import import TextImporter


class TextExtractionInput(Strict):
    text: str = Field(min_length=1, max_length=500000)
    row_description: str = Field(min_length=1, max_length=4000)
    columns: list[ExtractionColumn] = Field(min_length=1, max_length=24)
    dataset_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    primary_key: list[str] | None = None
    record_mode: Literal["auto", "paragraph", "line", "document"] = "auto"
    max_rows: int = Field(default=200, ge=1, le=1000)
    limits: dict | None = None
    commit: bool = False


def mount(app, db, decisions, reviewer):
    importer = TextImporter(db, decisions)
    app.state.text_importer = importer

    @app.post("/data/extractions", tags=["Text import"])
    def extract(body: TextExtractionInput, p=Depends(reviewer)):
        """Extract exact source spans according to row/column descriptions.

        Set commit=true to create or append entries automatically when all rows are
        complete. Otherwise inspect the preview and commit its token later.
        """
        if importer.decisions is None:
            raise HTTPException(503, "JEV is not configured")
        arguments = body.model_dump(exclude={"commit"})
        preview = importer.preview(p["tenant"], p["name"], **arguments)
        if body.commit and preview["can_import"]:
            preview["import"] = importer.commit(p["tenant"], p["name"], preview["preview_token"])
            preview["committed"] = True
        return preview

    @app.post("/data/extractions/{preview_token}/commit", tags=["Text import"])
    def commit(preview_token: str, p=Depends(reviewer)):
        """Import a complete preview once; retries return its original result."""
        return importer.commit(p["tenant"], p["name"], preview_token)
