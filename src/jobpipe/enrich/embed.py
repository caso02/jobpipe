"""Embeddings für Inserate und Suchprofile.

Zwei Anbieter hinter einer Schnittstelle:

* **Gemini** (``gemini-embedding-001``) wenn ``GOOGLE_API_KEY`` gesetzt ist
* **sentence-transformers** lokal, sonst — kostenlos, offline, etwas schwächer

Jedes Embedding wird über einen Hash aus Modell, Dimension, Task-Typ und Text
gecacht. Ein Inserat wird nie zweimal eingebettet, auch nicht über Profile
hinweg: die Inseratsvektoren sind profil-unabhängig, nur die Profilvektoren
unterscheiden sich.

Zwei Fallstricke, beide gemessen
--------------------------------
1. **Matryoshka-Kürzung entnormalisiert.** Fordert man von Gemini 768 statt
   der vollen 3072 Dimensionen, kommen Vektoren mit Norm ~0.59 zurück. Ohne
   erneute L2-Normalisierung ist das Skalarprodukt keine Cosine-Ähnlichkeit
   mehr und alle Scores sind verzerrt.

2. **Deutsche Bürotexte liegen eng beieinander.** Gemessen:
   "Sachbearbeiterin Innendienst Auftragsabwicklung" gegen
   "Empfangsmitarbeiterin Frontdesk Telefonzentrale" ergibt Cosine **0.86** —
   obwohl das genau die Unterscheidung ist, auf die es bei Profil B ankommt.
   Rohe Cosine-Werte taugen deshalb nicht als Score. :func:`spread_scores`
   normalisiert innerhalb des Kandidatenpools, damit aus einem Band von
   0.80-0.90 wieder eine nutzbare Rangfolge wird.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import structlog

log = structlog.get_logger(__name__)

GEMINI_MODEL = "gemini-embedding-001"
LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

#: 768 reicht für ein paar tausend Inserate und hält die Datenbank klein.
DEFAULT_DIM = 768

#: Symmetrischer Vergleich: Inserat gegen Zielrolle, keine Frage-Antwort-Suche.
TASK_TYPE = "SEMANTIC_SIMILARITY"

#: Gemini nimmt mehrere Texte pro Request. Konservativ gewählt, damit ein
#: Fehlschlag wenig Arbeit vernichtet.
BATCH_SIZE = 50

#: Zeichen, ab denen gekürzt wird. Stellenbeschreibungen sind selten länger
#: informativ; der Rest ist meist Firmenwerbung.
MAX_CHARS = 6000


class EmbeddingError(RuntimeError):
    """Der Anbieter konnte keine Vektoren liefern."""


class Embedder(Protocol):
    # Nur lesend: ``FallbackEmbedder`` reicht beide als Property durch, weil
    # sie sich beim Modellwechsel mitändern.
    @property
    def name(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]: ...


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2-Normalisierung. Pflicht nach Matryoshka-Kürzung."""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype("float32")


def truncate(text: str, limit: int = MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit]


# --------------------------------------------------------------------------
# Anbieter
# --------------------------------------------------------------------------


class GeminiEmbedder:
    name = GEMINI_MODEL

    def __init__(self, dim: int = DEFAULT_DIM, api_key: str | None = None) -> None:
        from google import genai

        self.dim = dim
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        out: list[np.ndarray] = []
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = [truncate(t) or " " for t in texts[start : start + BATCH_SIZE]]
            out.extend(self._embed_chunk(chunk))
        return out

    def _embed_chunk(self, chunk: list[str], attempts: int = 4) -> list[np.ndarray]:
        delay = 2.0
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.models.embed_content(
                    model=GEMINI_MODEL,
                    contents=list(chunk),  # type: ignore[arg-type]
                    config={
                        "task_type": TASK_TYPE,
                        "output_dimensionality": self.dim,
                    },
                )
            except Exception as exc:  # SDK wirft je nach Fehler unterschiedlich
                last = exc
                if attempt == attempts - 1:
                    break
                log.warning("embed.retry", attempt=attempt + 1, error=str(exc)[:120])
                time.sleep(delay)
                delay *= 2
                continue
            if not resp.embeddings:
                raise EmbeddingError("Gemini antwortete ohne Embeddings")
            # Ohne diese Normalisierung ist das Skalarprodukt keine Cosine.
            return [l2_normalize(np.array(e.values, dtype="float32")) for e in resp.embeddings]
        raise EmbeddingError(f"Gemini lieferte keine Embeddings: {last}") from last


class LocalEmbedder:
    """Offline-Fallback ohne API-Key und ohne Kosten."""

    name = LOCAL_MODEL

    def __init__(self, dim: int | None = None) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(LOCAL_MODEL)
        # Methodenname hat sich in sentence-transformers 5.x geändert.
        get_dim = (
            getattr(self._model, "get_embedding_dimension", None)
            or self._model.get_sentence_embedding_dimension
        )
        self.dim = dim or int(get_dim())

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        arr = self._model.encode(
            [truncate(t) or " " for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [np.asarray(v, dtype="float32") for v in arr]


class FallbackEmbedder:
    """Versucht Gemini, weicht bei Kontingentproblemen auf das lokale Modell aus.

    Der kostenlose Gemini-Tier für ``gemini-embedding-001`` ist eng: gemessen
    kam schon beim zweiten Batch ein ``429 RESOURCE_EXHAUSTED``. Ein Abbruch
    wäre die falsche Antwort — die Pipeline soll ein Ergebnis liefern, notfalls
    mit dem schwächeren, aber kostenlosen Modell.

    Der Wechsel gilt für den Rest des Laufs, damit nicht bei jedem Batch erneut
    ins Limit gelaufen wird. Bereits berechnete Gemini-Vektoren bleiben im
    Cache gültig; sie stehen dort unter einem anderen Schlüssel als die
    lokalen, es werden also nie zwei Modelle vermischt.
    """

    def __init__(self, primary: Embedder, make_fallback: Callable[[], Embedder]) -> None:
        self._primary = primary
        self._make_fallback = make_fallback
        self._fallback: Embedder | None = None
        self.switched = False

    @property
    def _active(self) -> Embedder:
        if self._fallback is not None:
            return self._fallback
        return self._primary

    @property
    def name(self) -> str:
        return self._active.name

    @property
    def dim(self) -> int:
        return self._active.dim

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if self._fallback is None:
            try:
                return self._primary.embed(texts)
            except EmbeddingError as exc:
                log.warning(
                    "embed.fallback",
                    reason=str(exc)[:160],
                    from_model=self._primary.name,
                    to_model=LOCAL_MODEL,
                )
                self._fallback = self._make_fallback()
                self.switched = True
        return self._active.embed(texts)


def build_embedder(
    prefer_local: bool = True,
    dim: int = DEFAULT_DIM,
    use_gemini: bool = False,
) -> Embedder:
    """Wählt den Anbieter.

    Standard ist **lokal**: kostenlos, offline, kein Kontingent. Gemini nur auf
    ausdrücklichen Wunsch und dann mit automatischem Rückfall — der kostenlose
    Tier reicht für ein paar tausend Inserate nicht aus.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not use_gemini or prefer_local:
        return LocalEmbedder()
    if not api_key:
        log.info("embed.no_api_key", fallback=LOCAL_MODEL)
        return LocalEmbedder()
    return FallbackEmbedder(GeminiEmbedder(dim=dim, api_key=api_key), LocalEmbedder)


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def cache_key(model: str, dim: int, text: str) -> str:
    basis = f"{model}|{dim}|{TASK_TYPE}|{truncate(text)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass
class EmbeddingStats:
    requested: int = 0
    from_cache: int = 0
    computed: int = 0


class EmbeddingCache:
    """Persistenter Cache. Spart bei jedem Lauf fast alle API-Aufrufe."""

    def __init__(self, conn: sqlite3.Connection, embedder: Embedder) -> None:
        self.conn = conn
        self.embedder = embedder
        self.stats = EmbeddingStats()

    def _load(self, keys: Sequence[str]) -> dict[str, np.ndarray]:
        found: dict[str, np.ndarray] = {}
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                f"SELECT cache_key, vector FROM embeddings WHERE cache_key IN ({marks})",
                chunk,
            ):
                found[row["cache_key"]] = np.frombuffer(row["vector"], dtype="float32")
        return found

    def _store(self, items: list[tuple[str, str, np.ndarray]]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO embeddings (cache_key, model, dim, vector)
               VALUES (?, ?, ?, ?)""",
            [(k, self.embedder.name, len(v), v.tobytes()) for k, _txt, v in items],
        )

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        """Vektoren für Texte, aus dem Cache oder frisch berechnet."""
        self.stats.requested += len(texts)
        keys = [cache_key(self.embedder.name, self.embedder.dim, t) for t in texts]
        cached = self._load(keys)

        missing_idx = [i for i, k in enumerate(keys) if k not in cached]
        self.stats.from_cache += len(texts) - len(missing_idx)

        if missing_idx:
            fresh = self.embedder.embed([texts[i] for i in missing_idx])
            self.stats.computed += len(fresh)
            new_items: list[tuple[str, str, np.ndarray]] = []
            for i, vec in zip(missing_idx, fresh, strict=True):
                cached[keys[i]] = vec
                new_items.append((keys[i], texts[i], vec))
            self._store(new_items)

        return [cached[k] for k in keys]


# --------------------------------------------------------------------------
# Ähnlichkeit
# --------------------------------------------------------------------------


def cosine_max(job_vec: np.ndarray, role_vecs: Sequence[np.ndarray]) -> tuple[float, int]:
    """Beste Übereinstimmung über alle Zielrollen.

    Das Maximum, nicht der Mittelwert: wer sich in mehrere Richtungen bewirbt,
    will pro Inserat wissen, ob **eine** Richtung passt. Ein Mittelwert würde
    ein perfektes Data-Engineering-Inserat abwerten, nur weil es nicht auch zur
    Rolle "Business Analyst" passt.
    """
    if not len(role_vecs):
        return 0.0, -1
    sims = np.array([float(job_vec @ r) for r in role_vecs])
    best = int(np.argmax(sims))
    return float(sims[best]), best


def spread_scores(values: Sequence[float]) -> list[float]:
    """Dehnt einen engen Wertebereich auf 0..1.

    Gemessen liegen die Cosine-Werte deutschsprachiger Stellentexte in einem
    schmalen Band (0.80-0.90 zwischen Sachbearbeitung und Empfang, obwohl das
    fachlich weit auseinander liegt). Roh verwendet, dominieren die Regeln den
    Gesamtscore vollständig und der semantische Anteil wird bedeutungslos.

    Rang-basierte Normalisierung statt Min-Max: sie ist robust gegen einzelne
    Ausreisser und liefert eine gleichmässige Verteilung.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    for rank, idx in enumerate(order):
        out[idx] = rank / (n - 1)
    return out


def build_job_text(title: str, company: str, description: str) -> str:
    """Der Text, der eingebettet wird.

    Titel doppelt gewichtet, indem er vorangestellt und im Fliesstext
    wiederholt wird — er trägt das meiste Signal, geht in einer langen
    Beschreibung aber sonst unter.
    """
    parts = [title, f"{title} bei {company}.", description]
    return "\n\n".join(p for p in parts if p).strip()


def build_role_text(name: str, description: str) -> str:
    return f"{name}\n\n{description}".strip()


def as_matrix(vectors: Sequence[np.ndarray]) -> np.ndarray:
    return np.vstack(vectors) if vectors else np.zeros((0, 0), dtype="float32")


def describe(embedder: Embedder) -> dict[str, Any]:
    return {"model": embedder.name, "dim": embedder.dim, "task_type": TASK_TYPE}
