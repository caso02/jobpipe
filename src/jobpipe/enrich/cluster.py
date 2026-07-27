"""Clustering der Job-Typen.

Beantwortet die Frage, die eine Trefferliste nicht beantwortet: **welche Arten
von Stellen gibt es hier überhaupt?** Das Ranking sagt, was am besten passt;
das Clustering zeigt die Landschaft drumherum — auch Nischen, an die man beim
Formulieren der Zielrollen nicht gedacht hat.

Zwei Entscheidungen, die zählen
-------------------------------
**UMAP vor HDBSCAN.** HDBSCAN arbeitet auf 768 Dimensionen schlecht: dort ist
alles ungefähr gleich weit von allem entfernt, und Dichteunterschiede
verschwinden. Erst die Reduktion auf ~12 Dimensionen macht Dichte wieder
messbar.

**Labeling zweigleisig.** TF-IDF-Terme allein ergeben Cluster-Namen wie
"python, daten, cloud" — technisch korrekt, aber nicht lesbar. Deshalb
zusätzlich der nächste Nachbar unter den 1'851 Schweizer Berufen aus
``berufe_ch.json`` (dem Datensatz aus dem DSP-Vorprojekt). Damit heisst ein
Cluster "Informatik → Dateningenieur/in" statt "Cluster 7".
"""

from __future__ import annotations

import json
import sqlite3
import warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger(__name__)

#: Zieldimension der UMAP-Reduktion. Genug, um Struktur zu erhalten, wenig
#: genug, damit Dichte wieder aussagekräftig ist.
UMAP_COMPONENTS = 12
UMAP_NEIGHBORS = 15

#: Kleinere Gruppen sind bei ein paar tausend Inseraten Zufall.
MIN_CLUSTER_SIZE = 25
MIN_SAMPLES = 5

#: Wie viele charakteristische Begriffe je Cluster.
TOP_TERMS = 6

#: Deutsche Füllwörter plus Stellenanzeigen-Floskeln. Ohne sie bestünden die
#: Cluster-Begriffe aus "und", "für", "wir", "bieten".
STOPWORDS = frozenset(
    [
        "und",
        "oder",
        "aber",
        "der",
        "die",
        "das",
        "dem",
        "den",
        "des",
        "ein",
        "eine",
        "einer",
        "eines",
        "einem",
        "einen",
        "ist",
        "sind",
        "war",
        "waren",
        "sein",
        "haben",
        "hat",
        "hatte",
        "wird",
        "werden",
        "wurde",
        "für",
        "von",
        "mit",
        "bei",
        "aus",
        "auf",
        "zu",
        "zum",
        "zur",
        "im",
        "in",
        "am",
        "an",
        "als",
        "auch",
        "nach",
        "über",
        "unter",
        "vor",
        "durch",
        "sowie",
        "sich",
        "ihre",
        "ihr",
        "unser",
        "unsere",
        "du",
        "dich",
        "dir",
        "sie",
        "wir",
        "uns",
        "ihnen",
        "man",
        "wenn",
        "dass",
        "damit",
        "sowohl",
        "als",
        "auch",
        "nicht",
        "kein",
        "keine",
        "mehr",
        "sehr",
        "gut",
        "gute",
        "guten",
        "neue",
        "neuen",
        "erste",
        "ersten",
        "wir",
        "bieten",
        "suchen",
        "bringst",
        "dein",
        "deine",
        "stelle",
        "stellen",
        "job",
        "jobs",
        "arbeit",
        "arbeiten",
        "mitarbeiter",
        "mitarbeiterin",
        "aufgaben",
        "profil",
        "anforderungen",
        "angebot",
        "bewerbung",
        "team",
        "unternehmen",
        "firma",
        "prozent",
        "pensum",
        "region",
        "schweiz",
        "ch",
        "ag",
        "gmbh",
        "sa",
    ]
)


@dataclass
class Cluster:
    label: int
    size: int
    terms: list[str] = field(default_factory=list)
    beruf: str | None = None
    berufsfeld: str | None = None
    beruf_similarity: float = 0.0
    examples: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Offizielles Berufsfeld plus charakteristische Begriffe.

        Die **konkrete Berufsbezeichnung** wird bewusst nicht in den Namen
        aufgenommen, obwohl sie ermittelt wird. Gemessen ist sie unzuverlässig:
        ein Cluster mit den Begriffen "logistiker, lager, umschlag" bekam als
        nächsten Beruf "Bedienungs- und Schalterpersonal (Seilbahnen/Skilifte)"
        — dasselbe Berufsfeld, aber die falsche Rolle darin.
        Das Berufsfeld ist grob genug, um zu stimmen; die Begriffe sind
        spezifisch genug, um etwas zu sagen. Beides zusammen trägt, die
        Zwischenebene nicht.
        """
        terms = ", ".join(self.terms[:3])
        if self.berufsfeld and terms:
            return f"{self.berufsfeld} — {terms}"
        if self.berufsfeld:
            return self.berufsfeld
        return terms or f"Cluster {self.label}"


@dataclass
class ClusterReport:
    n_clusters: int = 0
    noise: int = 0
    total: int = 0
    clusters: list[Cluster] = field(default_factory=list)

    @property
    def noise_share(self) -> float:
        return round(100 * self.noise / self.total, 1) if self.total else 0.0


def reduce_dimensions(vectors: np.ndarray, seed: int = 42) -> np.ndarray:
    """UMAP auf ``UMAP_COMPONENTS`` Dimensionen.

    ``metric="cosine"``, weil die Embeddings L2-normalisiert sind und der
    Winkel die Bedeutung trägt, nicht die Länge.
    """
    import umap

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reducer = umap.UMAP(
            n_components=min(UMAP_COMPONENTS, vectors.shape[1] - 1),
            n_neighbors=min(UMAP_NEIGHBORS, max(2, len(vectors) - 1)),
            min_dist=0.0,
            metric="cosine",
            random_state=seed,
        )
        result: np.ndarray = reducer.fit_transform(vectors)
    return result


def find_clusters(reduced: np.ndarray, min_cluster_size: int = MIN_CLUSTER_SIZE) -> np.ndarray:
    from sklearn.cluster import HDBSCAN

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        labels: np.ndarray = HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=MIN_SAMPLES,
        ).fit_predict(reduced)
    return labels


#: Ein Begriff muss in mindestens so vielen Inseraten des Clusters vorkommen.
#:
#: Ohne diese Untergrenze gewinnen Zufallsbegriffe: der reine Häufigkeits-
#: überschuss krönte Wörter wie "thornton", "nacelles" und "gmsa" — sie kommen
#: im Gesamtbestand genau einmal vor und haben deshalb ein unschlagbares
#: Verhältnis, sagen über den Cluster aber nichts.
MIN_DOC_SHARE = 0.10


def characteristic_terms(
    texts_in_cluster: list[str], all_texts: list[str], top: int = TOP_TERMS
) -> list[str]:
    """Begriffe, die den Cluster von den übrigen Stellen unterscheiden.

    Kombiniert zwei Bedingungen, weil jede allein danebengreift: reine
    Häufigkeit liefert "erfahrung" und "kenntnisse", reiner Überschuss
    gegenüber dem Rest liefert Einmal-Vorkommnisse. Gefordert ist beides —
    der Begriff muss in einem nennenswerten Teil der Cluster-Inserate stehen
    UND dort deutlich häufiger sein als anderswo.
    """
    import math
    import re

    word_re = re.compile(r"[a-zäöüß]{4,}", re.IGNORECASE)

    def doc_counts(texts: list[str]) -> Counter[str]:
        """In wie vielen Dokumenten kommt das Wort vor (nicht wie oft total)."""
        c: Counter[str] = Counter()
        for t in texts:
            c.update({w for w in word_re.findall(t.lower()) if w not in STOPWORDS})
        return c

    inside = doc_counts(texts_in_cluster)
    overall = doc_counts(all_texts)
    n_in, n_all = len(texts_in_cluster), len(all_texts)
    if not inside or not n_in:
        return []

    min_docs = max(3, int(MIN_DOC_SHARE * n_in))
    scored: list[tuple[str, float]] = []
    for word, docs in inside.items():
        if docs < min_docs:
            continue
        share_in = docs / n_in
        share_all = overall[word] / n_all
        if share_all <= 0:
            continue
        # Anteil im Cluster mal logarithmischer Überschuss: häufig UND
        # unterscheidend.
        scored.append((word, share_in * math.log(share_in / share_all + 1e-9)))

    scored.sort(key=lambda x: -x[1])
    return [w for w, s in scored[:top] if s > 0]


#: Wie viele nächste Berufe abstimmen dürfen.
BERUF_VOTES = 12

#: So einig muss sich die Abstimmung über das Berufsfeld sein.
#:
#: Ein einzelner nächster Nachbar ist unzuverlässig: das lokale Modell gibt
#: auch unverwandten Paaren hohe Werte (gemessen 0.86 zwischen
#: "Sachbearbeiterin Innendienst" und "Empfangsmitarbeiterin Frontdesk"), ein
#: fester Ähnlichkeitsschwellwert trennt darum nichts. Ein Logistik-Cluster
#: landete so bei "Bedienungs- und Schalterpersonal (Seilbahnen)".
#:
#: Die Mehrheit unter den zwölf nächsten Berufen ist stabiler. Erreicht sie
#: diesen Anteil nicht, bleibt der Cluster unbenannt — ein falsches Etikett
#: ist schlechter als keines.
MIN_FIELD_AGREEMENT = 0.4


def label_with_berufe(
    conn: sqlite3.Connection,
    cluster_titles: dict[int, list[str]],
    embed_fn: Any,
) -> dict[int, tuple[str, str | None, float]]:
    """Ordnet jedem Cluster den ähnlichsten Schweizer Beruf zu.

    **Titel gegen Titel, nicht Schwerpunkt gegen Beschreibung.** Der erste
    Versuch verglich den Cluster-Schwerpunkt (gemittelt über ganze
    Stelleninserate) mit den Berufsbeschreibungen der Taxonomie. Beides sind
    völlig verschiedene Textsorten — lange Werbetexte gegen kurze
    Lexikoneinträge —, und der nächste Nachbar über diese Kluft hinweg war
    praktisch Zufall: ein Logistik-Cluster hiess "Bedienungs- und
    Schalterpersonal (Seilbahnen)", ein Finanzcluster "Kundenassistent/in SBB".

    Stellentitel gegen Berufsbezeichnungen ist dieselbe Textsorte, gleiche
    Länge, gleicher Zweck. Bleibt die Ähnlichkeit trotzdem niedrig, wird gar
    kein Beruf zugeordnet — ein falsches Etikett ist schlechter als keines.
    """
    rows = conn.execute("SELECT title, berufsfeld FROM berufe WHERE title IS NOT NULL").fetchall()
    if not rows or not cluster_titles:
        return {}

    beruf_vecs = np.vstack(embed_fn([r["title"] for r in rows]))

    labels = list(cluster_titles)
    # Repräsentativer Titeltext je Cluster: die häufigsten Stellentitel.
    profiles = ["; ".join(cluster_titles[lab][:12]) for lab in labels]
    profile_vecs = np.vstack(embed_fn(profiles))

    out: dict[int, tuple[str, str | None, float]] = {}
    for label, vec in zip(labels, profile_vecs, strict=True):
        sims = beruf_vecs @ vec
        top_idx = np.argsort(sims)[::-1][:BERUF_VOTES]

        # Berufsfeld per Mehrheit unter den nächsten Nachbarn.
        votes = Counter(rows[int(i)]["berufsfeld"] for i in top_idx if rows[int(i)]["berufsfeld"])
        if not votes:
            continue
        field_name, count = votes.most_common(1)[0]
        agreement = count / len(top_idx)
        if agreement < MIN_FIELD_AGREEMENT:
            continue

        # Innerhalb des gewählten Berufsfelds den ähnlichsten Beruf nehmen.
        in_field = [int(i) for i in top_idx if rows[int(i)]["berufsfeld"] == field_name]
        best = max(in_field, key=lambda i: sims[i])
        out[label] = (rows[best]["title"], field_name, round(agreement, 2))
    return out


def analyse(
    conn: sqlite3.Connection,
    job_ids: list[int],
    vectors: np.ndarray,
    texts: list[str],
    titles: list[str],
    embed_fn: Any = None,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> ClusterReport:
    """Vollständiger Durchlauf: reduzieren, clustern, benennen, speichern."""
    report = ClusterReport(total=len(job_ids))
    if len(job_ids) < min_cluster_size * 2:
        log.warning("cluster.too_few", n=len(job_ids))
        return report

    reduced = reduce_dimensions(vectors)
    labels = find_clusters(reduced, min_cluster_size)

    report.noise = int((labels == -1).sum())
    unique = sorted({int(x) for x in labels if x != -1})
    report.n_clusters = len(unique)

    # Titelprofil je Cluster: die häufigsten Stellentitel, normalisiert.
    # Grundlage der Berufszuordnung — Titel gegen Titel statt Fliesstext
    # gegen Lexikoneintrag.
    from jobpipe.parse.schema import normalize_title

    cluster_titles: dict[int, list[str]] = {}
    for lab in unique:
        titles_in = [titles[i] for i in range(len(labels)) if labels[i] == lab]
        common = Counter(normalize_title(t) for t in titles_in if t)
        cluster_titles[lab] = [t for t, _ in common.most_common(12) if t]

    beruf_labels = label_with_berufe(conn, cluster_titles, embed_fn) if embed_fn else {}

    for lab in unique:
        mask = labels == lab
        idx = [i for i, m in enumerate(mask) if m]
        cluster = Cluster(
            label=lab,
            size=int(mask.sum()),
            terms=characteristic_terms([texts[i] for i in idx], texts),
            examples=[titles[i] for i in idx[:3]],
        )
        if lab in beruf_labels:
            cluster.beruf, cluster.berufsfeld, cluster.beruf_similarity = beruf_labels[lab]
        report.clusters.append(cluster)

    report.clusters.sort(key=lambda c: -c.size)
    _persist(conn, job_ids, labels, report)
    return report


def _persist(
    conn: sqlite3.Connection,
    job_ids: list[int],
    labels: np.ndarray,
    report: ClusterReport,
) -> None:
    by_label = {c.label: c for c in report.clusters}
    conn.execute("DELETE FROM job_clusters")
    conn.executemany(
        "INSERT INTO job_clusters (job_rowid, cluster_label, cluster_name) VALUES (?,?,?)",
        [
            (jid, int(lab), by_label[int(lab)].name if int(lab) in by_label else None)
            for jid, lab in zip(job_ids, labels, strict=True)
            if int(lab) != -1
        ],
    )
    conn.execute("DELETE FROM clusters")
    conn.executemany(
        """INSERT INTO clusters (label, name, size, terms, beruf, berufsfeld, similarity)
           VALUES (?,?,?,?,?,?,?)""",
        [
            (
                c.label,
                c.name,
                c.size,
                json.dumps(c.terms, ensure_ascii=False),
                c.beruf,
                c.berufsfeld,
                c.beruf_similarity,
            )
            for c in report.clusters
        ],
    )
