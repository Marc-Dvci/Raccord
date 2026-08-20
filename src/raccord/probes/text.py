"""Deterministic text models used by the caption and description probes.

Two small, fully local models:

* a character n-gram language identifier, fitted on the authorised programme
  corpus, used to catch a track carrying the wrong language;
* a hashed character-n-gram embedding with cosine similarity, used as the
  semantic-preservation signal.

Both are deliberately small and deterministic so that every score in the
benchmark is reproducible on a laptop with no model download, and both are
honest about what they are: the similarity signal is lexical overlap, not
meaning, and the language profiles are fitted on this repository's own programme
corpus. docs/model_card.md states the limits and the measured accuracy.

A production profile swaps the embedding for a multilingual encoder behind the
same three functions - `embed`, `similarity`, `identify_language` - which is the
whole reason the probes depend on the interface rather than on the model.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

import numpy as np

from .. import media

EMBED_DIM = 256
_WORD_RE = re.compile(r"[a-z0-9'\-]+")


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def tokenise(text: str) -> list[str]:
    return _WORD_RE.findall(normalise(text))


# ---------------------------------------------------------------------------
# Language identification
# ---------------------------------------------------------------------------


def _ngrams(text: str, n: int = 3) -> list[str]:
    padded = f"  {normalise(text)}  "
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


@lru_cache(maxsize=1)
def _language_profiles() -> dict[str, dict[str, float]]:
    """Fit trigram profiles from the authorised programme corpus."""
    profiles: dict[str, dict[str, float]] = {}
    for lang in media.SUPPORTED_LANGUAGES:
        corpus = " ".join(line.text.get(lang, "") for line in media.SCRIPT)
        counts = Counter(_ngrams(corpus))
        total = sum(counts.values()) or 1
        profiles[lang] = {g: c / total for g, c in counts.items()}
    return profiles


def identify_language(text: str) -> tuple[str, float]:
    """Return (language, confidence). Confidence is a normalised log-likelihood
    margin over the runner-up, clipped to 0..1."""
    if len(normalise(text)) < 12:
        return "unknown", 0.0
    profiles = _language_profiles()
    grams = _ngrams(text)
    scores: dict[str, float] = {}
    for lang, profile in profiles.items():
        ll = 0.0
        for g in grams:
            ll += math.log(profile.get(g, 1e-7))
        scores[lang] = ll / max(1, len(grams))
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else best_score - 1.0
    margin = best_score - runner_up
    confidence = float(min(1.0, max(0.0, margin / 1.2)))
    return best, confidence


# ---------------------------------------------------------------------------
# Hashed embedding + similarity
# ---------------------------------------------------------------------------


def embed(text: str, dim: int = EMBED_DIM) -> np.ndarray:
    """Hashed character-4-gram + word unigram embedding, L2 normalised."""
    vec = np.zeros(dim, dtype=np.float32)
    norm = normalise(text)
    if not norm:
        return vec
    for g in _ngrams(norm, 4):
        vec[hash_bucket(g, dim)] += 1.0
    for w in tokenise(norm):
        vec[hash_bucket("w:" + w, dim)] += 1.5
    n = float(np.linalg.norm(vec))
    return vec / n if n else vec


def hash_bucket(token: str, dim: int) -> int:
    h = 2166136261
    for ch in token:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h % dim


def similarity(a: str, b: str) -> float:
    va, vb = embed(a), embed(b)
    if not va.any() or not vb.any():
        return 0.0
    return float(np.clip(np.dot(va, vb), 0.0, 1.0))


def reading_speed_cps(text: str, duration_s: float) -> float:
    if duration_s <= 0:
        return float("inf")
    return len(normalise(text)) / duration_s
