# Extract database entries from text

Describe what one row represents and what belongs in each column. `JEV.EXTRACT_TABLE` finds records, selects source spans, parses typed values and checks their grounding. `POST /data/extractions` adds a preview and transactional insertion into a new or existing dataset. This is an SDD composite built from JEV Choice and Noul primitives, not a provider API that generates JSON or SQL.

## Use the workspace

Open `/ask/en` or `/ask/zh`, connect with a reviewer token, and expand Extract entries from text under Manage data. The [Simplified Chinese guide](zh/USER_GUIDE.md) describes the Chinese interface.

1. Choose a new dataset name or an existing writable destination.
2. Explain one row, for example “one supplier delivery per row.”
3. Add columns with names, types and descriptions. Describe units, whether signs matter, and what distinguishes competing values. Existing column descriptions are loaded automatically.
4. Paste the source or load a UTF-8 `.txt` file. Choose automatic boundaries, one record per line/paragraph, or one record for the whole document.
5. Extract and inspect the proposed entries and source excerpts. Choose Import these entries, or enable automatic import before extraction.

The Simplified Chinese interface uses the same operator. Descriptions, column names and source text may be Chinese. A row explanation defines the meaning of every record; you do not enter individual records by hand.

## API example

Requires a configured JEV key and a database reviewer token. They are separate credentials. Send this JSON to `POST /data/extractions` with `Authorization: Bearer <database-token>`:

```json
{
  "name": "deliveries",
  "row_description": "One supplier delivery per row",
  "record_mode": "line",
  "columns": [
    {"name": "supplier", "type": "text", "description": "Supplier name"},
    {"name": "quantity", "type": "integer", "description": "Number of valves"},
    {"name": "amount", "type": "number", "description": "Total charge in dollars"}
  ],
  "text": "Northwind sent 12 valves, total $1,250.50.\nEastbank sent 8 valves, total $840.00.",
  "commit": true
}
```

`commit: true` inserts only when the entire requested extraction is resolved. The response includes `committed`, `can_import`, `preview_token`, `extraction` and, after success, `import.dataset_id` and `import.inserted_rows`. If a field or record is uncertain, the response retains the partial proposal and inserts nothing.

For review first, omit `commit` or set it to `false`. Later send `{}` to `POST /data/extractions/{preview_token}/commit`. A preview expires after one hour, belongs to its tenant and requesting actor, and can be committed only once. Retrying that commit returns the original result. Repeating extraction creates a new preview and can append duplicates; it is not a deduplication operation.

To append, replace `name` with `dataset_id`. Field names and types must match that destination. Describe every required column and primary-key column. For new datasets, optional `primary_key` specifies a list of column names; otherwise the catalog adds an internal row identifier. An insertion failure rolls back the whole import. Existing entries are never updated by this function.

Run [`examples/operators/text_import.py`](../examples/operators/text_import.py) for preview mode. Add `--commit` to insert automatically when resolved.

## Column contract

| Setting | Meaning |
|---|---|
| `name`, `description` | Destination name and a natural-language explanation of its value |
| `type` | `text`, `integer`, `number` or `date` |
| `nullable` | Whether the new destination column allows NULL; default false |
| `on_missing` | Default `hold`; `null` explicitly permits NULL when JEV finds the value unstated and `nullable` is true |
| `decimal_separator`, `group_separator` | Defaults `.` and `,`; choose explicitly for other numeric conventions |
| `percent` | `reject` by default, `points` for 12% → 12, or `fraction` for 12% → 0.12 |

Text is copied verbatim from an inclusive-start/exclusive-end source span. Offsets count Unicode code points, not UTF-8 bytes or JavaScript UTF-16 units. Each cell includes its source text and offsets; the result includes the source SHA-256. Numbers use deterministic decimal conversion, never an LLM calculation. Supported notation includes signed numbers, grouped digits, scientific notation, currency prefixes, parentheses for negative amounts, thousand/million/billion and 万/亿. Date storage accepts explicit ISO dates or Chinese calendar dates. Unsupported or ambiguous formats remain unresolved; the operator does not infer exchange rates, dates or missing numbers.

Integers must fit signed 64-bit storage. Decimal numbers must fit `NUMERIC(38,10)`. Use a text column to retain more precision. Import compares database-returned values with the proposed typed values and rolls back if storage changes them; SQLite may reject large decimals that PostgreSQL preserves exactly.

## States and evidence

- `VALUE`: a cell has a selected, parsed and grounded value.
- `UNKNOWN`: evaluated but absent, ambiguous, invalid or insufficiently supported.
- `NOT_EVALUATED`: a dependency, provider or budget prevented evaluation.

Execution status is separate, including `FAILED`, `BLOCKED_BY_BUDGET` and `TRUNCATED`. A skipped field never becomes false or zero. An explicitly permitted NULL remains an `UNKNOWN` cell with `null_by_policy: true`; it is an authorized blank assignment, not a known factual value. Such a row can be importable even though raw observation coverage is incomplete. Use `can_import` and `manifest.answer_complete`, not observation coverage alone, to determine import readiness.

For extraction without insertion, call `/jev/call` with `operator: "EXTRACT_TABLE"` and arguments `text`, `columns`, `row_description`, optional `record_mode` and `max_rows`. It returns the same evidence through the regular operator run ledger.

## Bounds and limitations

The request accepts up to 500,000 characters, 24 columns and 4,000 passages. `max_rows` defaults to 200 and can be set up to 1,000. Each record is bounded to 120 passages and 16,000 characters, with the runtime's stricter byte-based context checks still applying. Default work budgets include 1,000 judgments; raise `limits` only within the existing operator policy. Long documents can exhaust those limits. Partial results are retained, and no incomplete batch is imported. Split larger jobs at record boundaries when needed.

Automatic segmentation can hold cross-sentence references, multiple records in one sentence, or values split across passage boundaries. Choose explicit line/paragraph boundaries where the document provides them. Multiple new records within a declared single-record block are held rather than silently merged. Text selection is model-dependent: verbatim provenance and checked numeric conversion do not guarantee perfect semantic accuracy. Inspect the preview for consequential imports.

Extraction identifies records before mapping their fields. Independent fields and records can share requests and run concurrently within the configured limits. Import waits for a complete, typed and grounded batch.
