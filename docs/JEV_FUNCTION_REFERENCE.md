# JEV function reference

Each section gives the `arguments` object for `POST /jev/call`. Set `operator` to the section name:

```json
{
  "operator": "JEV.NOUL",
  "arguments": {
    "state": "The work is complete.",
    "proposition": "The source reports completed work."
  }
}
```

Use your database bearer token. Most examples run on supplied text; examples with `<placeholders>` require IDs from earlier operations. All calls retain a run/evidence record. REVIEW, PROMOTE, MATERIALIZE and REFRESH require a reviewer token; DISCOVER saves provisional definitions.

Inspect `output_state` before reading `value`. Incomplete results use `partial_value`; observations keep their individual output and operation states. Model-dependent examples illustrate inputs and result shapes, not guaranteed labels. Shared budgets, authorization and state rules are in the [operator guide](JEV_OPERATORS.md).

Optional execution controls belong beside `operator` and `arguments`, for example `"limits": {"max_judgments": 100, "concurrency": 4}`. Do not pass the outer budget as an operator argument. Most population parameters also accept `{"dataset_id":"...","where":{"team":"North"}}`; registered datasets require a primary key. Direct states for NOUL, CHOICE, SCORE and COMPARE are literal input values.

The same complete request examples are available from `GET /jev/operators/examples` and each catalog entry's `usage` field. The source is [examples.json](../sdd/operators/examples.json); rebuild this reference with `python -m scripts.build_operator_docs`.


[PROMPT](#jevprompt) · [NOUL](#jevnoul) · [CHOICE](#jevchoice) · [SCORE](#jevscore) · [CLASSIFY](#jevclassify) · [TAG](#jevtag) · [FILTER](#jevfilter) · [RANK](#jevrank) · [COMPARE](#jevcompare) · [RERANK](#jevrerank) · [COMPOSITE_SCORE](#jevcomposite_score) · [EXTRACT](#jevextract) · [EXTRACT_DATE](#jevextract_date) · [FIND](#jevfind) · [STRUCTURE](#jevstructure) · [ROUTE](#jevroute) · [JOIN](#jevjoin) · [ALIGN](#jevalign) · [VERIFY](#jevverify) · [AGGREGATE](#jevaggregate) · [SUMMARY_EXTRACTIVE](#jevsummary_extractive) · [RELATE](#jevrelate) · [COVER](#jevcover) · [TRACE](#jevtrace) · [DISCOVER](#jevdiscover) · [CONTRAST](#jevcontrast) · [RESOLVE](#jevresolve) · [STATE_SCAN](#jevstate_scan) · [MATCH](#jevmatch) · [EVIDENCE_JOIN](#jevevidence_join) · [EVALUATE](#jevevaluate) · [ENSURE_SEMANTICS](#jevensure_semantics) · [MATERIALIZE](#jevmaterialize) · [REFRESH](#jevrefresh) · [REVIEW](#jevreview) · [PROMOTE](#jevpromote) · [SELECT_SCHEMA](#jevselect_schema) · [PLAN_SQL](#jevplan_sql) · [EXPLAIN_PLAN](#jevexplain_plan) · [CLASSIFY_HIERARCHY](#jevclassify_hierarchy) · [WORKFLOW](#jevworkflow) · [EXTRACT_TABLE](#jevextract_table)


## JEV.PROMPT

Ask a typed question over one or more subjects.

`prompt(subjects, instructions, output_type, criteria=None)`

Apply the same typed question to several subjects without writing a separate operator for each task.

```json
{
  "subjects": [
    "The requested work was completed yesterday."
  ],
  "instructions": "The source reports completed work, rather than a promise or request.",
  "output_type": "noul"
}
```

Returns: A decision per subject. output_type is noul, choice, or score; the latter two require criteria.

## JEV.NOUL

Evaluate one proposition about a supplied state.

`noul(state, proposition, criteria=None)`

Test whether supplied evidence supports one proposition, while preserving an unresolved answer.

```json
{
  "state": "The requested work was completed yesterday.",
  "proposition": "The source reports completed work, rather than a promise or request."
}
```

Returns: A Boolean decision in value["0"], or an unresolved observation with its raw probability.

## JEV.CHOICE

Choose from described, caller-supplied options.

`choice(state, question, options)`

Choose one of a small set of alternatives with explicit descriptions.

```json
{
  "state": "Please send the updated file.",
  "question": "Which category fits the message?",
  "options": {
    "request": "Asks someone to act",
    "update": "Reports a status",
    "unknown": "Insufficient information"
  }
}
```

Returns: The selected option ID; inspect the observation for probabilities.

## JEV.SCORE

Rate a subject against ordered levels.

`score(state, rubric_levels, instructions='Rate the subject against the supplied rubric')`

Assess intensity or quality against ordered levels; use SQL for exact arithmetic.

```json
{
  "state": "The requested work was completed yesterday.",
  "rubric_levels": [
    "No completed work",
    "Ambiguous completion",
    "Explicitly completed work"
  ]
}
```

Returns: A numeric ordinal score from 0 to 2 for this three-level rubric.

## JEV.CLASSIFY

Assign subjects to a taxonomy.

`classify(subjects, taxonomy_rev)`

Assign each record to one described category.

```json
{
  "subjects": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "taxonomy_rev": {
    "instructions": "Classify the message purpose.",
    "options": {
      "request": "Asks someone to act",
      "update": "Reports a status"
    }
  }
}
```

Returns: One category per subject; an unknown option is added automatically.

## JEV.TAG

Evaluate several independent concepts for every subject.

`tag(subjects, concept_revs)`

Apply several independent labels when a record may match more than one.

```json
{
  "subjects": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "concept_revs": [
    "The source reports completed work, rather than a promise or request.",
    "The source asks for future action."
  ]
}
```

Returns: Subject IDs map to concept-indexed decisions; compatible questions share a context.

## JEV.FILTER

Separate matching, rejected and unresolved subjects.

`filter(relation, concept_rev, mode='exhaustive')`

Keep records that meet a semantic condition and track unresolved membership.

```json
{
  "relation": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "concept_rev": "The source reports completed work, rather than a promise or request."
}
```

Returns: matches, rejected and unresolved lists. The supplied population is evaluated exhaustively.

## JEV.RANK

Rank a population using pointwise evaluation.

`rank(subjects, criterion, method='pointwise', top_k=None)`

Prioritize a population with a shared rubric and preserve equal scores.

```json
{
  "subjects": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "criterion": {
    "type": "score",
    "instructions": "Rate how clearly the record establishes completed work.",
    "criteria": [
      "Absent",
      "Uncertain",
      "Explicit"
    ]
  },
  "top_k": 1
}
```

Returns: ranked rows with scores and competition ranks; ties at the cutoff remain included.

## JEV.COMPARE

Compare two supplied values directly.

`compare(left, right, criterion)`

Compare two supplied items under one criterion.

```json
{
  "left": "Finished and verified.",
  "right": "Scheduled for tomorrow.",
  "criterion": "Which source more clearly establishes completed work?"
}
```

Returns: A left, right or tie decision; insufficient evidence remains UNKNOWN.

## JEV.RERANK

Reorder an existing retrieval shortlist.

`rerank(query, candidates, criterion, top_k=10)`

Reorder an existing shortlist using a more specific criterion.

```json
{
  "query": "completed work",
  "candidates": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "criterion": "The source reports completed work, rather than a promise or request.",
  "top_k": 2
}
```

Returns: Ranked candidates with shortlist scope; this does not establish a corpus-wide top result.

## JEV.COMPOSITE_SCORE

Combine independently scored dimensions.

`composite_score(subjects, rubrics, weights, missing_policy='unknown')`

Combine several explicit rubric dimensions with chosen weights.

```json
{
  "subjects": [
    "The requested work was completed yesterday."
  ],
  "rubrics": {
    "completion": {
      "instructions": "Rate completion evidence.",
      "criteria": [
        "Absent",
        "Explicit"
      ]
    },
    "clarity": {
      "instructions": "Rate clarity of the status.",
      "criteria": [
        "Unclear",
        "Partial",
        "Clear"
      ]
    }
  },
  "weights": {
    "completion": 2,
    "clarity": 1
  },
  "missing_policy": "unknown"
}
```

Returns: Normalized composite and per-dimension decisions. Reweighting reuses compatible raw scores.

## JEV.EXTRACT

Select exact spans from a source.

`extract(subjects, field_spec, candidates=None, cardinality='one')`

Select source passages that satisfy a description without generating missing text.

```json
{
  "subjects": [
    {
      "id": "note",
      "text": "Work finished. Reference AB-42."
    }
  ],
  "field_spec": "Select the span containing the reference identifier.",
  "candidates": {
    "note": [
      {
        "start": 14,
        "end": 30
      }
    ]
  },
  "cardinality": "one"
}
```

Returns: Exact text, source ID and character offsets. Omit candidates to use sentence spans; use many for independent span decisions.

## JEV.EXTRACT_DATE

Normalize supported date expressions with explicit context.

`extract_date(subjects, reference_time=None, timezone=None, locale=None)`

Find a date in the source and normalize a supported explicit date format.

```json
{
  "subjects": [
    "The work finished yesterday."
  ],
  "reference_time": "2026-09-22T12:00:00+00:00",
  "timezone": "UTC",
  "locale": "en-GB"
}
```

Returns: Source spans with normalized dates. Missing context and unsupported expressions stay unresolved.

## JEV.FIND

Find relevant source sentences within a declared corpus.

`find(corpus, query, scope=None, max_spans=20, mode='exhaustive')`

Locate passages relevant to a question before further interpretation.

```json
{
  "corpus": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "query": "The source reports completed work, rather than a promise or request.",
  "max_spans": 10
}
```

Returns: Source spans. Pass a registered dataset scope as corpus when needed; scope remains null and mode exhaustive.

## JEV.STRUCTURE

Group adjacent source lines and classify the blocks.

`structure(lines, block_taxonomy)`

Group source passages into described sections while retaining their identity.

```json
{
  "lines": [
    "Status\n",
    "The work is complete.\n"
  ],
  "block_taxonomy": {
    "options": {
      "heading": "A section heading",
      "paragraph": "A prose paragraph"
    }
  }
}
```

Returns: Blocks with line offsets and type decisions; original source text round-trips exactly.

## JEV.ROUTE

Choose a handler and arguments from explicit allowlists.

`route(request, allowed_handlers, candidate_args=None)`

Choose an allowed handler for an input; dispatch remains the caller's responsibility.

```json
{
  "request": "Find recently completed work.",
  "allowed_handlers": {
    "lookup": "Search existing records",
    "summarize": "Select source excerpts"
  },
  "candidate_args": {
    "lookup": {
      "period": {
        "recent": "Recent records",
        "all": "All records"
      }
    }
  }
}
```

Returns: Handler and typed argument decisions. The handler is not executed.

## JEV.JOIN

Join two populations using a semantic predicate.

`join(left, right, predicate, candidate_policy=None, mode='exhaustive', join_type='inner')`

Find semantically related pairs across two supplied populations.

```json
{
  "left": [
    {
      "id": "a",
      "text": "The export failed."
    }
  ],
  "right": [
    {
      "id": "b",
      "text": "The export failure was fixed."
    }
  ],
  "predicate": "The right record addresses the same issue reported on the left.",
  "candidate_policy": {
    "kind": "all_pairs"
  },
  "join_type": "left"
}
```

Returns: Accepted edges, unresolved pairs and exhaustive unmatched rows. join_type also supports inner and anti.

## JEV.ALIGN

Propose identity matches and check companion fields.

`align(left, right, candidate_policy, identity_definition)`

Evaluate candidate identity matches without merging stored records.

```json
{
  "left": [
    {
      "id": "a",
      "name": "Project Orion"
    }
  ],
  "right": [
    {
      "id": "b",
      "name": "Orion project"
    }
  ],
  "candidate_policy": {
    "kind": "all_pairs"
  },
  "identity_definition": {
    "instructions": "The records name the same project.",
    "fields": [
      {
        "left": "name",
        "right": "name"
      }
    ]
  }
}
```

Returns: Identity and field-consistency decisions. No IDs are merged.

## JEV.VERIFY

Assess support and opposition for one claim.

`verify(claim, evidence, scope=None)`

Check whether evidence supports, opposes or leaves a claim unresolved.

```json
{
  "claim": "The work is complete.",
  "evidence": [
    "The requested work was completed yesterday.",
    "A later note says the work was reopened."
  ]
}
```

Returns: A support, opposition, conflict or insufficient stance, with separate evidence decisions.

## JEV.AGGREGATE

Run SQL arithmetic over accepted semantic labels.

`aggregate(relation, semantic_predicates, group_by=None, metric=None, mode='exhaustive')`

Compute a supported aggregate over evaluated decisions and retain coverage.

```json
{
  "relation": [
    {
      "id": "a",
      "team": "North",
      "amount": "12.30",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "team": "North",
      "amount": "5.20",
      "text": "Work is pending."
    }
  ],
  "semantic_predicates": [
    "The source reports completed work, rather than a promise or request."
  ],
  "group_by": [
    "team"
  ],
  "metric": {
    "kind": "sum",
    "field": "amount",
    "value_type": "decimal"
  }
}
```

Returns: Groups with complete and known-subset values, population counts and unresolved counts. Metrics: count, sum, avg, min, max; default null policy is ignore.

## JEV.SUMMARY_EXTRACTIVE

Select existing sentences for a requested facet.

`summary_extractive(subjects, facet, max_spans=10, diversity_policy='exact_dedup')`

Build a summary from selected original passages.

```json
{
  "subjects": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "facet": "Progress already completed.",
  "max_spans": 2,
  "diversity_policy": "per_source"
}
```

Returns: Source excerpts and omitted-candidate count. diversity_policy also supports exact_dedup.

## JEV.RELATE

Assess a directed relationship between supplied records.

`relate(left, right, relation_rev, evidence_policy=None, mode='exhaustive', candidate_policy=None)`

Identify described relationships with evidence tied to the source.

```json
{
  "left": [
    "The export fails on large files."
  ],
  "right": [
    "Large-file export failures were fixed."
  ],
  "relation_rev": "The right record asserts a fix for the left record."
}
```

Returns: Directed edges, separate support/opposition decisions and whole-source evidence offsets.

## JEV.COVER

Check obligations against an evidence population.

`cover(obligations, evidence_scope, relation_rev=None, mode='exhaustive')`

Check whether supplied evidence covers each declared requirement.

```json
{
  "obligations": [
    "The export must handle large files."
  ],
  "evidence_scope": [
    "Large-file exports passed the test."
  ]
}
```

Returns: A stance and supporting-source IDs for each obligation, scoped to the supplied evidence.

## JEV.TRACE

Reconstruct reported process states from ordered events.

`trace(records, entity_scope, process_rev, event_time)`

Identify a bounded event sequence in uniquely timestamped records.

```json
{
  "records": [
    {
      "id": "a",
      "item": "task-1",
      "at": "2026-09-22T09:00:00Z",
      "text": "The requested work was completed yesterday."
    }
  ],
  "entity_scope": "item",
  "event_time": "at",
  "process_rev": {
    "initial_state": "open",
    "events": {
      "completed": "Explicitly reports completed work",
      "promised": "Only promises future work"
    },
    "transitions": {
      "open": {
        "completed": "done",
        "promised": "open"
      },
      "done": {
        "completed": "done",
        "promised": "done"
      }
    }
  }
}
```

Returns: Per-entity timelines and possible/current states. Timestamps need offsets and unambiguous ordering.

## JEV.DISCOVER

Propose source-exemplar concepts for review.

`discover(records, catalog_rev, facet, discovery_budget=None)`

Propose reusable concepts from examples for later human review.

```json
{
  "records": [
    "The export times out.",
    "The export completes successfully."
  ],
  "catalog_rev": null,
  "facet": "Failure mode",
  "discovery_budget": {
    "max_concepts": 2,
    "neighbors": 2,
    "holdout": [
      "A fresh export attempt exceeded its time limit."
    ]
  }
}
```

Returns: Provisional candidate revisions and examples. Holdout content must be separate; promotion is a later reviewer action.

## JEV.CONTRAST

Compare concept rates across disjoint populations.

`contrast(left, right, concepts, unit, mode='exhaustive', discovery_split=None)`

Describe differences between declared populations without inferring causation.

```json
{
  "left": [
    {
      "id": "a",
      "unit": "item-1",
      "text": "The requested work was completed yesterday."
    }
  ],
  "right": [
    {
      "id": "b",
      "unit": "item-2",
      "text": "The work is pending."
    }
  ],
  "concepts": [
    "The source reports completed work, rather than a promise or request."
  ],
  "unit": "unit"
}
```

Returns: Rates, denominators, uncertainty bounds and descriptive differences; the declared unit must be unique and cohorts disjoint.

## JEV.RESOLVE

Stop adaptive evaluation when an answer is established.

`resolve(plan, objective, budget=None, wave_size=16)`

Stop evaluating once a declared decision condition is established.

```json
{
  "plan": {
    "subjects": [
      {
        "id": "a",
        "text": "The requested work was completed yesterday."
      },
      {
        "id": "b",
        "text": "Please finish the work tomorrow."
      }
    ],
    "predicate": "The source reports completed work, rather than a promise or request."
  },
  "objective": {
    "kind": "exists"
  },
  "wave_size": 1
}
```

Returns: An answer, count bounds and wave count; untouched subjects remain NOT_EVALUATED. Also supports count_at_least, count_at_most and exact_count.

## JEV.STATE_SCAN

Evaluate finite-state transitions in parallel under incoming-state hypotheses.

`state_scan(blocks, state_machine_rev, initial_state, uncertainty_policy='propagate_sets')`

Process transitions when the caller can describe sufficient state.

```json
{
  "blocks": [
    "Work is promised for tomorrow.",
    "The requested work was completed yesterday."
  ],
  "state_machine_rev": {
    "states": {
      "open": "Work remains unfinished",
      "done": "Work is completed"
    },
    "initial_state": "open",
    "instructions": "A promise does not establish completion.",
    "sufficient_history": "Current completion status contains all history required by these rules."
  },
  "initial_state": "open"
}
```

Returns: Conditional transition tables, composed summaries and possible states. Unknown transitions propagate sets.

## JEV.MATCH

Find bounded event patterns with explicit bindings.

`match(records, typed_pattern, candidate_policy=None, max_matches=100)`

Find bounded multi-record patterns with verified relationships.

```json
{
  "records": [
    {
      "id": "a",
      "item": "task-1",
      "step": 1,
      "text": "Work started."
    },
    {
      "id": "b",
      "item": "task-1",
      "step": 2,
      "text": "The requested work was completed yesterday."
    }
  ],
  "typed_pattern": {
    "nodes": [
      {
        "id": "start",
        "instructions": "Reports work starting."
      },
      {
        "id": "finish",
        "instructions": "Reports work finishing."
      }
    ],
    "edges": [
      {
        "left": "start",
        "right": "finish",
        "same": [
          "item"
        ],
        "before": "step"
      }
    ]
  },
  "max_matches": 10
}
```

Returns: Verified node-to-record bindings, candidate counts and truncation state.

## JEV.EVIDENCE_JOIN

Evaluate isolated evidence bundles for claims.

`evidence_join(claims, candidate_sources, max_bundle_size=2, scope=None)`

Combine evidence from several sources when no single source is sufficient.

```json
{
  "claims": [
    "The work is complete and verified."
  ],
  "candidate_sources": [
    "The work is complete.",
    "Verification passed."
  ],
  "max_bundle_size": 2
}
```

Returns: Support/opposition stances per singleton or pair bundle; at most eight candidate sources.

## JEV.EVALUATE

Submit explicit typed work items to the shared runtime.

`evaluate(work_items, limits=None, retry_policy=None, persist=True)`

Submit an explicit set of typed questions with common execution controls.

```json
{
  "work_items": [
    {
      "id": "completion",
      "subject_id": "note-1",
      "state": "The requested work was completed yesterday.",
      "source_revisions": [
        "note-1-v1"
      ],
      "question": {
        "type": "noul",
        "instructions": "The source reports completed work, rather than a promise or request."
      }
    }
  ]
}
```

Returns: Decisions keyed by work ID, with cache and budget accounting. Set budgets in the outer limits field.

## JEV.ENSURE_SEMANTICS

Fill or reuse semantic observations for subject and concept revisions.

`ensure_semantics(subject_revs, concept_revs, evaluator_revs=None, budget=None)`

Reuse compatible observations and schedule only missing semantic work.

```json
{
  "subject_revs": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "concept_revs": [
    "The source reports completed work, rather than a promise or request."
  ]
}
```

Returns: Concept decisions per subject. Optional evaluator_revs must match the configured pinned model.

## JEV.MATERIALIZE

Publish a generation for an approved concept and registered population.

`materialize(concept_rev, target_scope, refresh_policy, budget=None)`

Publish resolved values for an approved definition and registered source.

Requires: Reviewer token; use PROMOTE.value.approved_revision.id and an existing primary-key dataset ID.

```json
{
  "concept_rev": "<approved_revision>",
  "target_scope": {
    "dataset_id": "<dataset_id>"
  },
  "refresh_policy": {
    "mode": "explicit"
  }
}
```

Returns: A generation, published flag and observations. on_change requires max_refreshes (1–100) and interval_seconds (at least 30).

## JEV.REFRESH

Invalidate specified source dependencies and rebuild selected generations.

`refresh(change_set, policies=None, budget=None)`

Rebuild a materialized generation after its source dependencies change.

Requires: Reviewer token; use MATERIALIZE.value.generation.id. Dataset invalidation affects that whole dataset; use row revision hashes for narrower invalidation.

```json
{
  "change_set": {
    "source_revisions": [
      "<dataset_id>"
    ],
    "reason": "Source records changed."
  },
  "policies": [
    "<generation_id>"
  ]
}
```

Returns: Invalidated observation IDs and replacement generations, using the outer execution budget.

## JEV.REVIEW

Save a human assertion without overwriting the model evidence.

`review(observations, sampling_policy=None, reviewer_role=None)`

Record a human correction without replacing the original model observation.

Requires: Reviewer token; take observation_id from an evaluated run’s observations.

```json
{
  "observations": [
    {
      "observation_id": "<observation_id>",
      "value": false,
      "reason": "The source promises future work; it does not report completion."
    }
  ]
}
```

Returns: Saved assertions and confirmation that raw evidence is unchanged.

## JEV.PROMOTE

Approve a provisional concept using independently reviewed evidence.

`promote(candidate_rev, owner, validation_report, retention_policy)`

Approve a provisional definition after reviewing independent examples.

Requires: Reviewer token; use DISCOVER.value.candidates[0].candidate_revision and replace the sample validation report with your actual review.

```json
{
  "candidate_rev": "<candidate_revision>",
  "owner": "reviewer-name",
  "validation_report": {
    "independent_holdout": [
      "<reviewed_holdout_id>"
    ],
    "reviewed_examples": [
      {
        "text": "The work remains pending.",
        "expected": false
      }
    ]
  },
  "retention_policy": "Retain this approved definition for 30 days."
}
```

Returns: An approved revision ID; no backfill starts until MATERIALIZE.

## JEV.SELECT_SCHEMA

Select relevant authorized tables and fields.

`select_schema(request, authorized_catalog=None, candidate_policy=None)`

Select relevant catalog elements for a natural-language objective.

Requires: Existing dataset ID. Omit authorized_catalog to use the visible catalog within pilot limits.

```json
{
  "request": "Count completed records by team.",
  "authorized_catalog": [
    "<dataset_id>"
  ]
}
```

Returns: Selected tables/columns, catalog alternatives and approved relationship metadata.

## JEV.PLAN_SQL

Construct a staged SQL proposal for a natural-language request.

`plan_sql(request, catalog_rev=None, grammar_rev='staged-v1', mode='proposal')`

Obtain a SQL proposal for inspection without executing it.

Requires: Existing dataset ID. catalog_rev currently accepts the dataset-ID list used by the planner.

```json
{
  "request": "Count records by team.",
  "catalog_rev": [
    "<dataset_id>"
  ]
}
```

Returns: A reviewable plan and executed=false. A held proposal remains available; this function does not execute SQL.

## JEV.EXPLAIN_PLAN

Estimate missing semantic work before inference.

`explain_plan(typed_plan, source_stats=None, limits=None, cache_stats=None)`

Estimate required semantic work before dispatching provider calls.

```json
{
  "typed_plan": {
    "operator": "TAG",
    "stages": 1
  },
  "source_stats": {
    "subjects": 100,
    "questions": 2,
    "tokens_per_judgment": 300
  },
  "cache_stats": {
    "compatible_judgments": 20
  }
}
```

Returns: Judgments, conservative requests/cost bounds and policy warnings; this is an estimate, not an approval or guaranteed duration.

## JEV.CLASSIFY_HIERARCHY

Classify along bounded parallel taxonomy frontiers.

`classify_hierarchy(subjects, taxonomy_rev, beam_width=2, depth_cap=6)`

Select a category through a supplied hierarchy when one flat choice is unsuitable.

```json
{
  "subjects": [
    {
      "id": "a",
      "text": "The requested work was completed yesterday."
    },
    {
      "id": "b",
      "text": "Please finish the work tomorrow."
    }
  ],
  "taxonomy_rev": {
    "root": "message",
    "nodes": {
      "message": {
        "children": [
          "request",
          "update"
        ]
      },
      "request": {
        "description": "Requests future action"
      },
      "update": {
        "description": "Reports a status"
      }
    }
  },
  "beam_width": 2,
  "depth_cap": 3
}
```

Returns: Candidate paths, heuristic weights, dropped branches and unfinished paths.

## JEV.WORKFLOW

Run independent stages together and respect typed branch conditions.

`workflow(stages)`

Compose typed stages with explicit dependencies and conditional branches.

```json
{
  "stages": [
    {
      "id": "gate",
      "literal": false
    },
    {
      "id": "stage2",
      "when": {
        "stage": "gate",
        "equals": true
      },
      "state": "The requested work was completed yesterday.",
      "question": {
        "type": "noul",
        "instructions": "The source reports completed work, rather than a promise or request."
      }
    }
  ]
}
```

Returns: Stage decisions and execution layers. In this example stage2 is NOT_EVALUATED / SKIPPED and no provider call is needed.

## JEV.EXTRACT_TABLE

Create typed, source-backed rows using descriptions of records and columns.

`extract_table(text, columns, row_description, record_mode='auto', max_rows=200)`

Turn a document into typed rows using descriptions of records and columns.

```json
{
  "text": "Supplier Northwind supplied 12 valves for $1,250.50.\nSupplier Eastbank supplied 8 valves for $840.00.",
  "row_description": "One row for each supplier delivery.",
  "record_mode": "line",
  "columns": [
    {
      "name": "supplier",
      "type": "text",
      "description": "The supplier name, without the word Supplier."
    },
    {
      "name": "quantity",
      "type": "integer",
      "description": "The number of valves delivered."
    },
    {
      "name": "amount",
      "type": "number",
      "description": "The total amount charged for the delivery."
    }
  ]
}
```

Returns: Rows with exact source offsets, typed values and per-cell states. Missing or uncertain values remain unresolved. Four stages run by default. See TEXT_IMPORT.md for automatic insertion.
