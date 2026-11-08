"""The clinic's knowledge base: what Nightingale is allowed to say.

Clinic staff write markdown files in backend/kb/. Each `## section` becomes a
chunk that remembers exactly where it sits in its file (start/end character
offsets) and which version of the file it came from (content hash). A citation
is therefore never "the model said so": it is a span you can open and read.

Retrieval is BM25 over stemmed words, with a few synonyms. No embeddings and
no extra service: the corpus is small, and keyword matching is predictable
and easy to test. If nothing scores above the threshold, search returns
nothing and the assistant says it doesn't know instead of guessing.
"""

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KB_DIR = Path(__file__).parent.parent / "kb"

STOPWORDS = set("""
a an the and or but if of to in on at for with from by about as is are was were be been being am do does did
i me my you your we our us it its this that these those there here what which who whom how when where why
can could would should will shall may might must have has had having not no so than too very just also any
some such into over under again more most other only own same then once hi hello hey please thanks thank
""".split())

# Query words that should also match the clinic's own wording
SYNONYMS = {
    "price": ["cost"], "fee": ["cost"], "much": ["cost"], "expensive": ["cost"], "charge": ["cost"],
    "rm": ["cost"], "ringgit": ["cost"],
    "open": ["hour"], "time": ["hour"], "close": ["hour", "closed"], "weekend": ["saturday", "sunday"],
    "where": ["location"], "address": ["location"], "park": ["parking"],
    "oocyte": ["egg"], "freeze": ["freez"], "frozen": ["freez"],
    "appointment": ["book", "consultation"], "schedule": ["book"],
    "offer": ["servic"], "provide": ["servic"], "treatment": ["servic"],
    "reply": ["nurse"], "respond": ["nurse"], "wait": ["nurse", "reply"],
    "pregnant": ["conceiv", "pregnancy"], "baby": ["conceiv"],
}
MIN_SCORE = 2.0          # below this, we'd rather say "I don't know"
RELATIVE_CUTOFF = 0.45   # keep chunks scoring at least this fraction of the best


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


@dataclass(frozen=True)
class Chunk:
    id: str           # "egg-freezing.md#3"
    doc: str          # "kb/egg-freezing.md"
    title: str
    heading: str
    text: str         # exactly file_text[start:end]
    start: int
    end: int
    version: str      # content hash of the file when loaded


def _chunks_from_file(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    version = hashlib.sha256(raw.encode()).hexdigest()[:12]
    title_match = re.search(r"^# (.+)$", raw, re.M)
    title = title_match.group(1).strip() if title_match else path.stem
    headings = list(re.finditer(r"^## (.+)$", raw, re.M))
    chunks = []
    for n, h in enumerate(headings):
        body_start = h.end() + 1
        body_end = headings[n + 1].start() if n + 1 < len(headings) else len(raw)
        body = raw[body_start:body_end]
        # Trim whitespace but keep offsets exact
        lead = len(body) - len(body.lstrip())
        start, end = body_start + lead, body_start + len(body.rstrip())
        if end > start:
            chunks.append(Chunk(id=f"{path.name}#{n + 1}", doc=f"kb/{path.name}", title=title,
                                heading=h.group(1).strip(), text=raw[start:end], start=start, end=end,
                                version=version))
    return chunks


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._docs = [Counter(tokens(f"{c.title} {c.heading} {c.heading} {c.text}")) for c in chunks]
        self._lengths = [sum(d.values()) for d in self._docs]
        self._avg = (sum(self._lengths) / len(self._lengths)) if chunks else 1.0
        df = Counter(term for d in self._docs for term in d)
        n = len(chunks)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_dir(cls, directory: Path = KB_DIR) -> "KnowledgeBase":
        return cls([c for p in sorted(directory.glob("*.md")) for c in _chunks_from_file(p)])

    def _query_terms(self, query: str) -> list[str]:
        terms = tokens(query)
        # Synonyms are looked up on raw words: "where" is a stopword but still means "location"
        for word in re.findall(r"[a-z0-9]+", query.lower()):
            terms += [stem(s) for s in SYNONYMS.get(word, SYNONYMS.get(stem(word), []))]
        return terms

    def scored(self, query: str) -> list[tuple[float, Chunk]]:
        terms = self._query_terms(query)
        k1, b = 1.5, 0.75
        results = []
        for chunk, doc, length in zip(self.chunks, self._docs, self._lengths):
            score = 0.0
            for t in set(terms):
                f = doc.get(t, 0)
                if f:
                    score += self._idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * length / self._avg))
            results.append((score, chunk))
        return sorted(results, key=lambda r: -r[0])

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        ranked = self.scored(query)
        if not ranked or ranked[0][0] < MIN_SCORE:
            return []
        cutoff = max(MIN_SCORE, ranked[0][0] * RELATIVE_CUTOFF)
        return [c for s, c in ranked[:k] if s >= cutoff]


@lru_cache(maxsize=1)
def default_kb() -> KnowledgeBase:
    return KnowledgeBase.from_dir()
