"""Linear semantic operators: typed judgments first, deterministic reductions second."""

import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .types import Decision, WorkItem
from .types import OutputState as Out


def question(kind, instructions, criteria=None):
    result = {"type": kind, "instructions": instructions}
    if criteria is not None:
        result["criteria"] = criteria
    return result


class CoreOperators:
    def assess(self, subjects, questions, hypotheses=None):
        work = []
        for subject in subjects:
            for key, q in questions.items():
                work.append(
                    WorkItem(
                        subject["id"] + ":" + key,
                        subject["state"],
                        q,
                        subject["id"],
                        tuple(subject["revisions"]),
                        (hypotheses or {}).get(key, {}),
                    )
                )
        return self.runtime.evaluate(work)

    def prompt(self, subjects, instructions, output_type, criteria=None):
        """Ask a typed question over one or more subjects."""
        subjects = self.subjects(subjects)
        decisions = self.assess(subjects, {"answer": question(output_type, instructions, criteria)})
        return self.result(
            {s["id"]: decisions[s["id"] + ":answer"].json() for s in subjects}, decisions
        )

    def noul(self, state, proposition, criteria=None):
        """Evaluate one proposition about a supplied state."""
        return self.prompt([state], proposition, "noul", criteria)

    def choice(self, state, question, options):
        """Choose from described, caller-supplied options."""
        return self.prompt([state], question, "choice", options)

    def score(
        self, state, rubric_levels, instructions="Rate the subject against the supplied rubric"
    ):
        """Rate a subject against ordered levels."""
        return self.prompt([state], instructions, "score", rubric_levels)

    def classify(self, subjects, taxonomy_rev):
        """Assign subjects to a taxonomy."""
        taxonomy = self.definition(taxonomy_rev)
        options = taxonomy.get("options", taxonomy.get("criteria"))
        if not isinstance(options, dict):
            raise ValueError("Taxonomy revision requires described options")
        options = {**options, "unknown": "Outside this taxonomy or insufficient evidence"}
        return self.prompt(
            subjects,
            taxonomy.get("instructions", "Choose the single applicable category"),
            "choice",
            options,
        )

    def tag(self, subjects, concept_revs):
        """Evaluate several independent concepts for every subject."""
        subjects = self.subjects(subjects)
        questions = {}
        for index, reference in enumerate(concept_revs):
            definition = self.definition(reference)
            questions[str(index)] = question(
                definition.get("type", "noul"),
                definition["instructions"],
                definition.get("criteria"),
            )
        decisions = self.assess(subjects, questions)
        return self.result(
            {
                s["id"]: {key: decisions[s["id"] + ":" + key].json() for key in questions}
                for s in subjects
            },
            decisions,
        )

    def filter(self, relation, concept_rev, mode="exhaustive"):
        """Separate matching, rejected and unresolved subjects."""
        if mode != "exhaustive":
            raise ValueError(
                "FILTER evaluates the declared scope exhaustively; sampling is unsupported"
            )
        subjects = self.subjects(relation)
        definition = self.definition(concept_rev)
        decisions = self.assess(
            subjects,
            {"predicate": question("noul", definition["instructions"], definition.get("criteria"))},
        )
        matches = []
        rejected = []
        unresolved = []
        for subject in subjects:
            decision = decisions[subject["id"] + ":predicate"]
            if decision.output_state != Out.VALUE:
                target = unresolved
            elif decision.value is True:
                target = matches
            else:
                target = rejected
            target.append(subject)
        return self.result(
            {"matches": matches, "rejected": rejected, "unresolved": unresolved},
            decisions,
            mode=mode,
        )

    def rank(self, subjects, criterion, method="pointwise", top_k=None):
        """Rank a population using pointwise evaluation."""
        if method != "pointwise":
            raise ValueError("RANK is pointwise; use explicit COMPARE pairs for pairwise work")
        subjects = self.subjects(subjects)
        criterion = self.definition(criterion)
        kind = criterion.get("type", "score" if "criteria" in criterion else "noul")
        if kind not in {"noul", "score"}:
            raise ValueError("Ranking needs a Noul or Score criterion")
        decisions = self.assess(
            subjects,
            {"score": question(kind, criterion["instructions"], criterion.get("criteria"))},
        )
        rows = []
        unresolved = []
        for s in subjects:
            d = decisions[s["id"] + ":score"]
            if d.raw is None or d.output_state != Out.VALUE:
                unresolved.append(s["id"])
                continue
            rating = d.raw["score"] if kind == "score" else d.raw["noul"]
            rows.append({"id": s["id"], "score": rating, "subject": s})
        rows.sort(key=lambda r: (-r["score"], r["id"]))
        previous = None
        rank = 0
        for i, row in enumerate(rows):
            if previous != row["score"]:
                rank = i + 1
            previous = row["score"]
            row["rank"] = rank
        if top_k is not None:
            self.positive_int(top_k, "top_k")
            rows = [r for r in rows if r["rank"] <= top_k]
        return self.result(
            {
                "ranked": rows,
                "unresolved": unresolved,
                "population": len(subjects),
                "ties": "included",
            },
            decisions,
        )

    def compare(self, left, right, criterion):
        """Compare two supplied values directly."""
        return self.prompt(
            [{"left": left, "right": right}],
            criterion,
            "choice",
            {
                "left": "Left better satisfies the criterion",
                "right": "Right better satisfies the criterion",
                "tie": "Both satisfy the criterion equally",
                "insufficient": "Evidence cannot resolve the comparison",
            },
        )

    def rerank(self, query, candidates, criterion, top_k=10):
        """Reorder an existing retrieval shortlist."""
        subjects = self.subjects(candidates)
        augmented = [{**s, "state": {"query": query, "candidate": s["state"]}} for s in subjects]
        result = self.rank(augmented, criterion, top_k=top_k)
        result["scope"] = {
            "kind": "retrieval_shortlist",
            "candidate_count": len(subjects),
            "corpus_top_k": False,
        }
        return result

    def composite_score(self, subjects, rubrics, weights, missing_policy="unknown"):
        """Combine independently scored dimensions."""
        if missing_policy not in {"unknown", "renormalize"}:
            raise ValueError(
                "Missing dimensions must remain unknown or use explicit renormalization"
            )
        if (
            set(rubrics) != set(weights)
            or not rubrics
            or any(
                type(w) not in (int, float) or not math.isfinite(w) or w < 0
                for w in weights.values()
            )
            or sum(weights.values()) <= 0
        ):
            raise ValueError(
                "Each rubric requires a finite nonnegative weight; total weight must be positive"
            )
        subjects = self.subjects(subjects)
        definitions = {k: self.definition(v) for k, v in rubrics.items()}
        decisions = self.assess(
            subjects,
            {
                k: question("score", d["instructions"], d["criteria"])
                for k, d in definitions.items()
            },
        )
        output = {}
        for s in subjects:
            dimensions = {k: decisions[s["id"] + ":" + k] for k in rubrics}
            valid = {
                k: d.value / (len(definitions[k]["criteria"]) - 1)
                for k, d in dimensions.items()
                if d.output_state == Out.VALUE
            }
            missing_dimensions = len(valid) < len(rubrics)
            available_weight = sum(weights[key] for key in valid)
            if missing_dimensions and missing_policy == "unknown" or not available_weight:
                combined = Decision.unknown("A weighted dimension is unresolved")
            else:
                combined = Decision.known(
                    sum(weights[key] * value for key, value in valid.items()) / available_weight
                )
            output[s["id"]] = {
                "composite": combined.json(),
                "dimensions": {k: d.json() for k, d in dimensions.items()},
                "weights": weights,
                "missing_policy": missing_policy,
            }
        return self.result(output, decisions)

    def extract(self, subjects, field_spec, candidates=None, cardinality="one"):
        """Select exact spans from a source."""
        if cardinality not in {"one", "many"}:
            raise ValueError("Extraction cardinality is one or many")
        subjects = self.subjects(subjects)
        items = []
        spans = {}
        for s in subjects:
            text = self.text(s)
            supplied = candidates.get(s["id"], []) if isinstance(candidates, dict) else candidates
            proposed = (
                supplied
                if supplied is not None
                else [
                    {"start": m.start(), "end": m.end()}
                    for m in re.finditer(r"[^\n.!?。！？]+(?:[.!?。！？]|$)", text)
                ]
            )
            spans[s["id"]] = self.validate_spans(text, proposed)
            options = {str(i): text[p["start"] : p["end"]] for i, p in enumerate(spans[s["id"]])}
            if cardinality == "one":
                if len(options) > 254:
                    raise ValueError("Extraction needs at most 254 candidates plus unknown")
                q = question(
                    "choice",
                    field_spec,
                    {**options, "unknown": "No supplied source span answers this field"},
                )
                if options:
                    items.append(WorkItem(s["id"], s["state"], q, s["id"], tuple(s["revisions"])))
            else:
                for key, span in options.items():
                    q = question(
                        "noul",
                        {
                            "field": field_spec,
                            "candidate_span": span,
                            "question": "Does this exact span supply the requested value in context?",
                        },
                    )
                    items.append(
                        WorkItem(s["id"] + ":" + key, s["state"], q, s["id"], tuple(s["revisions"]))
                    )
        decisions = self.runtime.evaluate(items)
        output = {}
        for s in subjects:
            selected = []
            for index, span in enumerate(spans[s["id"]]):
                d = decisions.get(s["id"] if cardinality == "one" else s["id"] + ":" + str(index))
                if (
                    d is not None
                    and d.output_state == Out.VALUE
                    and (d.value == str(index) if cardinality == "one" else d.value is True)
                ):
                    selected.append(
                        {
                            **span,
                            "text": self.text(s)[span["start"] : span["end"]],
                            "subject_id": s["id"],
                        }
                    )
            if not spans[s["id"]]:
                decisions[s["id"]] = Decision.unknown("No candidate spans were supplied or found")
            output[s["id"]] = selected
        return self.result(
            output,
            decisions,
            candidate_recall="Limited to supplied/generated spans; no-match does not prove absence outside them",
        )

    @staticmethod
    def validate_spans(text, spans):
        output = []
        for span in spans:
            start, end = span.get("start"), span.get("end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                raise ValueError("Source spans require valid character offsets")
            if "text" in span and span["text"] != text[start:end]:
                raise ValueError("Span text differs from source")
            output.append({"start": start, "end": end})
        return output

    def extract_date(self, subjects, reference_time=None, timezone=None, locale=None):
        """Normalize supported date expressions with explicit context."""
        if timezone is None or locale is None:
            return self.result(
                None, {"date": Decision.unknown("Explicit timezone and locale are required")}
            )
        zone = ZoneInfo(timezone)
        if (
            reference_time
            and datetime.fromisoformat(reference_time.replace("Z", "+00:00")).utcoffset() is None
        ):
            raise ValueError("Reference time requires an explicit UTC offset")
        base = (
            datetime.fromisoformat(reference_time.replace("Z", "+00:00")).astimezone(zone)
            if reference_time
            else None
        )
        subjects = self.subjects(subjects)
        output = {}
        decisions = {}
        weekday = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
            "周一": 0,
            "周二": 1,
            "周三": 2,
            "周四": 3,
            "周五": 4,
            "周六": 5,
            "周日": 6,
        }
        pattern = r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\d{4}年\d{1,2}月\d{1,2}日|today|yesterday|tomorrow|今天|昨天|明天|last (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|上周[一二三四五六日]"
        for s in subjects:
            values = []
            for i, m in enumerate(re.finditer(pattern, self.text(s), re.I)):
                raw = m.group()
                resolved = None
                try:
                    if re.match(r"\d{4}[-年]", raw):
                        parts = list(map(int, re.findall(r"\d+", raw)))
                        resolved = datetime(*parts, tzinfo=zone).date()
                    elif "/" in raw:
                        a, b, y = map(int, raw.split("/"))
                        if locale not in {"en-US", "en-GB"}:
                            raise ValueError("Ambiguous date locale")
                        resolved = (
                            datetime(y, a, b, tzinfo=zone).date()
                            if locale == "en-US"
                            else datetime(y, b, a, tzinfo=zone).date()
                        )
                    elif base:
                        offsets = {
                            "today": 0,
                            "今天": 0,
                            "yesterday": -1,
                            "昨天": -1,
                            "tomorrow": 1,
                            "明天": 1,
                        }
                        if raw.lower() in offsets:
                            resolved = (base + timedelta(days=offsets[raw.lower()])).date()
                        else:
                            day = weekday.get(
                                raw.lower().removeprefix("last "),
                                weekday.get(raw.removeprefix("上")),
                            )
                            resolved = (
                                base - timedelta(days=(base.weekday() - day) % 7 or 7)
                            ).date()
                    d = (
                        Decision.known(resolved.isoformat())
                        if resolved
                        else Decision.unknown("Reference time is required")
                    )
                except (ValueError, TypeError):
                    d = Decision.unknown("Invalid or ambiguous date expression")
                decisions[s["id"] + ":" + str(i)] = d
                values.append(
                    {
                        "span": {"start": m.start(), "end": m.end(), "text": raw},
                        "date": d.json(),
                        "timezone": timezone,
                        "locale": locale,
                    }
                )
            if not values:
                decisions[s["id"]] = Decision.unknown("No supported date expression found")
            output[s["id"]] = values
        return self.result(
            output,
            decisions,
            parser_scope="ISO, slash dates with declared locale, Chinese dates and bounded relative-day/weekday expressions",
        )

    def find(self, corpus, query, scope=None, max_spans=20, mode="exhaustive"):
        """Find relevant source sentences within a declared corpus."""
        if mode != "exhaustive" or scope is not None:
            raise ValueError(
                "FIND scope is the supplied corpus; use a dataset scope as corpus, with exhaustive mode"
            )
        result = self.extract(corpus, query, cardinality="many")
        spans = [span for values in result["value"].values() for span in values]
        self.positive_int(max_spans, "max_spans")
        result["value"] = {"spans": spans[:max_spans], "scope": scope, "mode": mode}
        if len(spans) > max_spans:
            result.update(complete=False, truncated=True)
        return result

    def summary_extractive(self, subjects, facet, max_spans=10, diversity_policy="exact_dedup"):
        """Select existing sentences for a requested facet."""
        self.positive_int(max_spans, "max_spans")
        if diversity_policy not in {"exact_dedup", "per_source"}:
            raise ValueError("Supported diversity policies are exact_dedup and per_source")
        sources = self.subjects(subjects)
        snippets = []
        seen = set()
        for source in sources:
            for m in re.finditer(r"[^\n.!?。！？]+(?:[.!?。！？]|$)", self.text(source)):
                text = m.group()
                if text in seen:
                    continue
                seen.add(text)
                snippets.append(
                    {
                        "id": source["id"] + ":" + str(m.start()),
                        "state": {
                            "text": text,
                            "source_id": source["id"],
                            "start": m.start(),
                            "end": m.end(),
                        },
                        "revisions": source["revisions"],
                    }
                )
        result = self.rank(snippets, {"instructions": facet, "type": "noul"})
        rows = result["value"]["ranked"]
        if diversity_policy == "per_source":
            seen = set()
            distinct_sources = []
            for row in rows:
                source_id = row["subject"]["state"]["source_id"]
                if source_id in seen:
                    continue
                seen.add(source_id)
                distinct_sources.append(row)
            rows = distinct_sources
        result["value"] = {
            "excerpts": [r["subject"]["state"] for r in rows[:max_spans]],
            "omitted_candidates": max(0, len(snippets) - min(len(rows), max_spans)),
            "representativeness_guaranteed": False,
        }
        return result
