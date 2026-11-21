"""Small shared utilities: atomic file writes, JSON helpers, text metrics."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + os.replace) so a crash never
    leaves a half-written store. Same pattern both upstream projects use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def jaccard(words_a: Iterable[str], words_b: Iterable[str]) -> float:
    """Jaccard similarity over word sets — the same dedup metric Shikigami
    Protocol uses for fact duplicates (>0.8 means duplicate)."""
    a = set(words_a)
    b = set(words_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_WORD_RE = re.compile(r"[a-z0-9']+")


def content_words(text: str) -> list[str]:
    """Lowercase word list used for duplicate detection and FTS queries."""
    return _WORD_RE.findall(text.lower())


STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "am", "i", "you", "he", "she",
    "it", "we", "they", "my", "your", "me", "him", "her", "them", "this",
    "that", "these", "those", "at", "by", "from", "as", "so", "do", "does",
    "did", "have", "has", "had", "will", "would", "can", "could", "not",
    "no", "yes", "about", "just", "very", "really", "also",
}


def _stem(word: str) -> str:
    """Very light stemming (Luna uses a custom stemmer for the same purpose):
    strips plural/progressive/past suffixes so 'play'/'playing' and
    'garden'/'gardens' compare equal in duplicate detection."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _stem_stable(word: str) -> str:
    """Stem to a fixpoint so 'morning'/'mornings' land on the same token."""
    prev = word
    for _ in range(4):
        nxt = _stem(prev)
        if nxt == prev:
            return nxt
        prev = nxt
    return prev


def salient_words(text: str) -> list[str]:
    """Stemmed content words with stopwords removed — used to compare facts."""
    return [_stem_stable(w) for w in content_words(text) if w not in STOPWORDS]


def new_id(prefix: str) -> str:
    """Short random id like Shikigami's fact ids (f_ + 8 hex)."""
    import secrets

    return f"{prefix}_{secrets.token_hex(4)}"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "person"
