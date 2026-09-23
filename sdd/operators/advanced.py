"""Source-anchored concept proposals and finite event-pattern matching."""

import re
from itertools import product

from . import schema
from .core import question
from .types import OutputState as Out
from .types import WorkItem


class AdvancedOperators:
    def discover(self, records, catalog_rev, facet, discovery_budget=None):
        """Propose source-exemplar concepts for review."""
        budget = discovery_budget or {}
        records = self.subjects(records)
        if len(records) > 5000:
            raise ValueError("Discovery sample is limited to 5,000 declared records")
        count = budget.get("max_concepts", 5)
        neighbors = budget.get("neighbors", 20)
        if not 1 <= count <= 20 or not 1 <= neighbors <= 20:
            raise ValueError("Discovery supports at most 20 proposals and 20 neighbors")
        holdout = self.subjects(budget.get("holdout", []))
        if {self.fingerprint(s["state"]) for s in records} & {
            self.fingerprint(s["state"]) for s in holdout
        }:
            raise ValueError("Discovery and holdout contain the same source content")
        catalog = self.definition(catalog_rev) if catalog_rev else {"options": {}}
        decisions = {}
        residuals = records
        if catalog.get("options"):
            known = self.assess(
                records,
                {
                    "known": question(
                        "choice",
                        {
                            "facet": facet,
                            "question": "Choose an existing category only if it covers this record's requested facet",
                        },
                        {**catalog["options"], "unknown": "Not covered by this catalog"},
                    )
                },
            )
            decisions.update(known)
            residuals = [s for s in records if known[s["id"] + ":known"].output_state != Out.VALUE]

        def tokens(s):
            return set(re.findall(r"\w+|[\u4e00-\u9fff]", self.text(s).casefold()))

        represented = []
        candidates = []
        for source in sorted(residuals, key=lambda s: self.fingerprint(s["state"])):
            current = tokens(source)
            if (
                represented
                and max(len(current & t) / max(1, len(current | t)) for t in represented) > 0.8
            ):
                continue
            represented.append(current)
            candidates.append(source)
            if len(candidates) >= count:
                break
        work = []
        definitions = []
        for i, exemplar in enumerate(candidates):
            definition = {
                "type": "noul",
                "instructions": {
                    "facet": facet,
                    "positive_exemplar": exemplar["state"],
                    "question": "Does this source exhibit the same specific facet as the exemplar? Shared keywords alone are insufficient.",
                },
                "exemplar_id": exemplar["id"],
                "status": "provisional",
            }
            definitions.append(definition)
            ordered = sorted(
                residuals, key=lambda s: (-len(tokens(s) & tokens(exemplar)), s["id"])
            )[:neighbors]
            for group, population in (("discovery", ordered), ("holdout", holdout)):
                for s in population:
                    work.append(
                        WorkItem(
                            f"{i}:{group}:{s['id']}",
                            s["state"],
                            question("noul", definition["instructions"]),
                            s["id"],
                            tuple(s["revisions"]),
                        )
                    )
        found = self.runtime.evaluate(work)
        decisions.update(found)
        output = []
        for i, definition in enumerate(definitions):
            row = self.store.add(
                schema.definitions,
                name="candidate-" + self.fingerprint(definition)[:12],
                owner=self.store.actor,
                definition=definition,
                status="PROVISIONAL",
                validation={
                    "holdout_subjects": [s["id"] for s in holdout],
                    "independent_human_quality": None,
                },
            )
            positive = []
            negative = []
            unresolved = []
            for key, decision in found.items():
                if not key.startswith(str(i) + ":"):
                    continue
                if decision.output_state != Out.VALUE:
                    unresolved.append(key)
                elif decision.value is True:
                    positive.append(key)
                else:
                    negative.append(key)
            output.append(
                {
                    "candidate_revision": row["id"],
                    "definition": definition,
                    "positive_examples": positive,
                    "near_misses": negative,
                    "unresolved": unresolved,
                    "promotion_required": True,
                }
            )
        return self.result(
            {
                "candidates": output,
                "catalog_unchanged": True,
                "backfill_started": False,
                "independent_validation_required": True,
            },
            decisions,
        )

    def match(self, records, typed_pattern, candidate_policy=None, max_matches=100):
        """Find bounded event patterns with explicit bindings."""
        self.positive_int(max_matches, "max_matches")
        if max_matches > 1000:
            raise ValueError("At most 1,000 verified matches per pilot")
        records = self.subjects(records)
        nodes = typed_pattern.get("nodes", [])
        edges = typed_pattern.get("edges", [])
        if not 1 <= len(nodes) <= 6 or len({n["id"] for n in nodes}) != len(nodes):
            raise ValueError("Pattern requires 1–6 unique typed event nodes")
        names = {n["id"] for n in nodes}
        if any(edge["left"] not in names or edge["right"] not in names for edge in edges):
            raise ValueError("Pattern edge refers to an unknown node")
        local = self.assess(records, {n["id"]: question("noul", n["instructions"]) for n in nodes})
        pools = {
            n["id"]: [
                s
                for s in records
                if local[s["id"] + ":" + n["id"]].output_state == Out.VALUE
                and local[s["id"] + ":" + n["id"]].value is True
            ]
            for n in nodes
        }
        max_neighbors = (candidate_policy or {}).get("max_neighbors", 20)
        if not 1 <= max_neighbors <= 20:
            raise ValueError("Pattern neighbor cap must be 1–20")
        proposed = []
        truncated = False
        counts = {}
        for inspected, assignment in enumerate(product(*(pools[n["id"]] for n in nodes))):
            if inspected >= 10000:
                truncated = True
                break
            if len({s["id"] for s in assignment}) != len(assignment):
                continue
            binding = dict(zip([n["id"] for n in nodes], assignment))
            valid = True
            for edge in edges:
                a, b = binding[edge["left"]], binding[edge["right"]]
                for field in edge.get("same", []):
                    va, vb = self.field(a, field), self.field(b, field)
                    if va is None or vb is None or va != vb:
                        valid = False
                if edge.get("before"):
                    field = edge["before"]
                    ta, tb = self.field(a, field), self.field(b, field)
                    if ta is None or tb is None or not ta < tb:
                        valid = False
            if not valid:
                continue
            anchor = assignment[0]["id"]
            counts[anchor] = counts.get(anchor, 0) + 1
            if counts[anchor] > max_neighbors:
                truncated = True
                continue
            proposed.append(binding)
            if len(proposed) >= 1000:
                truncated = True
                break
        work = []
        for i, binding in enumerate(proposed):
            state = {
                "bindings": {name: s["state"] for name, s in binding.items()},
                "pattern": typed_pattern,
            }
            q = question(
                "noul",
                "Does this complete anchored assignment satisfy the full pattern, including shared entities/objects, chronology, attribution, negation and modality? Local event matches alone are not enough.",
            )
            work.append(
                WorkItem(
                    str(i),
                    state,
                    q,
                    str(i),
                    tuple(v for s in binding.values() for v in s["revisions"]),
                )
            )
        verified = self.runtime.evaluate(work)
        decisions = {
            **{"local:" + k: d for k, d in local.items()},
            **{"verify:" + k: d for k, d in verified.items()},
        }
        matches = [
            {name: s["id"] for name, s in proposed[int(key)].items()}
            for key, d in verified.items()
            if d.output_state == Out.VALUE and d.value is True
        ]
        truncated |= len(matches) > max_matches
        return self.result(
            {
                "matches": matches[:max_matches],
                "verified_candidates": len(proposed),
                "neighbor_cap": max_neighbors,
                "pattern": typed_pattern,
            },
            decisions,
            truncated=truncated,
        )
