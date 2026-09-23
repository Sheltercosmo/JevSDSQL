# Performance and cost

These measurements apply to the planner and execution code in JevSDSQL 0.4.0. They describe a local comparison, not an official benchmark score or an expected result for every dataset.

## Measured results

Each method attempted the same 100 questions labelled challenging in the cleaned BIRD development set, across 11 databases. One reference query timed out, leaving 99 questions for accuracy. Held proposals were scored when SQL was available; all 100 attempts per method contributed to usage and cost.

The LLM baseline and hybrid used `gpt-5.6-terra` with low reasoning effort through the same local CLI transport. JEV used `jev-1.13.0`. SQL results were compared with references for columns, values, duplicates and applicable ordering.

| Measure | JEV | LLM baseline | Hybrid |
| --- | ---: | ---: | ---: |
| Matching SQL answers | 20/99 | 39/99 | 34/99 |
| Complete matching application results | 20/99 | 38/99 | 34/99 |
| Median request time | 8.55 s | 8.68 s | 14.23 s |
| 95th percentile request time | 26.80 s | 38.85 s | 54.16 s |
| Estimated cost per 100 attempts | $0.389 | $3.262 | $2.839 |
| LLM calls | 0 | 100 | 104 |
| JEV HTTP attempts | 2,623 | 0 | 732 |

Hybrid reduced estimated cost by about 13% relative to the LLM baseline. It answered fewer questions correctly and had about 64% higher median latency. This comparison does not establish an accuracy or speed advantage for hybrid.

## Interpreting the numbers

Timing covers 94 questions processed with up to three cases in flight. It includes planning, model transport, local CLI startup and application execution. It excludes database setup and reference execution. Provider load and transport choice can materially change latency.

The SQL score checks the complete generated query result. The application score additionally requires delivery without truncation. The application returns at most 1,000 rows, which accounts for the LLM baseline's lower application score.

All JEV and hybrid attempts required planning review. Their scores measure available proposals, not unattended completion. The workspace retains the user confirmation step. Invalid SQL and unresolved plans cannot be executed through confirmation.

Independent JEV work reached four concurrent provider requests. Batching also reduced transport work. There was no serial comparison, so these measurements do not quantify the speedup caused by parallel execution.

## Cost assumptions

Dollar figures apply the following fixed rates to recorded provider usage:

| Token category | Assumed USD per million tokens |
| --- | ---: |
| LLM input | 2.00 |
| LLM cached input | 0.20 |
| LLM output | 12.00 |
| LLM cache write | 2.50 |
| JEV input | 0.042 |

These are accounting assumptions, not current price quotations or subscription charges. One failed hybrid JEV request had no token usage record and is unpriced. The hybrid total therefore has incomplete accounting for that request; missing usage is not a known zero cost.

Estimated cost per matching SQL answer was $0.0194 for JEV, $0.0836 for the LLM baseline and $0.0835 for hybrid. These figures include the cost of unsuccessful attempts and should be read alongside accuracy.

## Controlling usage

Hybrid normally uses one LLM generation. Keep `SDD_HYBRID_CONCEPTS=off` to avoid a preliminary concept call. `SDD_HYBRID_REPAIR=on` permits one additional generation for an actionable defect; set it to `off` to disable that repair. In this measurement, 96 hybrid requests used one LLM call and four used two.

Set explicit `limits` for direct operator calls and inspect returned usage, coverage and completeness. `JEV.EXPLAIN_PLAN` estimates missing work before inference. Compatible evidence can be reused, while changes to data, model or meaning can require new calls. See [operator controls](JEV_OPERATORS.md#inputs-and-execution-controls) and [hybrid configuration](HYBRID_QUERY.md#configuration).
