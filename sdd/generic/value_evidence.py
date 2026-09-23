"""Bounded lexical retrieval supplements semantic decisions; it never chooses a filter."""

from difflib import SequenceMatcher
import re
import math


def value_relevance(request, candidate):
    words = re.findall(r"\w+", request.casefold())
    text = str(candidate).casefold()
    if text and re.search(r"(?<!\w)" + re.escape(text) + r"(?!\w)", request.casefold()):
        return 2.0 + len(text) / 1000
    parts = re.findall(r"\w+", text)
    scores = [
        max((SequenceMatcher(None, part, word).ratio() for word in words), default=0)
        for part in parts
        if len(part) >= 3
    ]
    return max(scores, default=0) + sum(scores) / max(1, len(scores)) / 10


def retrieval_tokens(request):
    return list(dict.fromkeys(word[:3] for word in re.findall(r"[a-zA-Z]{4,}", request.lower())))[
        :40
    ]


def rank_values(request, candidates):
    candidates = list(dict.fromkeys(candidates))
    words = list(dict.fromkeys(w for w in re.findall(r"\w+", request.casefold()) if len(w) >= 4))
    tokens = [re.findall(r"\w+", str(v).casefold()) for v in candidates]
    similarities = [
        [
            max((SequenceMatcher(None, word, part).ratio() for part in row), default=0)
            for word in words
        ]
        for row in tokens
    ]
    weights = [
        math.log(1 + len(candidates) / (1 + sum(row[i] >= 0.75 for row in similarities)))
        for i in range(len(words))
    ]
    scores = [
        sum(weights[i] * score**3 for i, score in enumerate(row) if score >= 0.75)
        for row in similarities
    ]
    return [
        candidates[i]
        for i in sorted(
            range(len(candidates)),
            key=lambda i: (
                -scores[i],
                -value_relevance(request, candidates[i]),
                str(candidates[i]),
            ),
        )
    ]
