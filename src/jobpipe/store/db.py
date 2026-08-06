"""SQLite-Storage.

Eine Datei, kein Server. Bei erwarteten <100k Zeilen völlig ausreichend, und
für ein Repo, das jemand in zwei Minuten klonen und starten können soll, die
richtige Wahl. Für Ad-hoc-Analysen liest DuckDB die Datei direkt.

Migrationen laufen über ``PRAGMA user_version`` — jede Migration ist eine
Funktion, die von Version N auf N+1 hebt.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 9


# --------------------------------------------------------------------------
# Verbindung
# --------------------------------------------------------------------------


def connect(db_path: Path) -> sqlite3.Connection:
    """Öffnet die DB und setzt die Pragmas, die man immer will."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

_MIGRATION_1 = """
-- Ein Inserat, portal-unabhängig normalisiert.
-- Bewusst NICHT enthalten: jegliche Kontaktperson. Siehe parse/pii.py.
CREATE TABLE jobs (
    id                  INTEGER PRIMARY KEY,
    portal              TEXT    NOT NULL,   -- job_room | myjob | ostjob | zentraljob
    source_id           TEXT    NOT NULL,
    source_url          TEXT    NOT NULL,
    cluster_id          TEXT,               -- portalübergreifende Dedup-Gruppe

    title               TEXT    NOT NULL,
    company_name        TEXT    NOT NULL,
    company_is_agency   INTEGER NOT NULL DEFAULT 0,
    description_md      TEXT    NOT NULL DEFAULT '',
    description_truncated INTEGER NOT NULL DEFAULT 0,

    city                TEXT,
    postal_code         TEXT,
    canton              TEXT,
    lat                 REAL,
    lon                 REAL,

    workload_min        INTEGER,
    workload_max        INTEGER,
    is_permanent        INTEGER,
    start_date          TEXT,
    home_office         INTEGER,
    salary_min          INTEGER,            -- praktisch immer NULL, kein Portal liefert es
    salary_max          INTEGER,

    posted_at           TEXT,
    expires_at          TEXT,
    status              TEXT    NOT NULL DEFAULT 'active',  -- active | expired

    apply_url           TEXT,
    occupation_codes    TEXT    NOT NULL DEFAULT '[]',      -- JSON-Array (AVAM)
    categories          TEXT    NOT NULL DEFAULT '[]',      -- JSON-Array
    languages           TEXT    NOT NULL DEFAULT '[]',      -- JSON-Array

    content_hash        TEXT,               -- für Near-Duplicate-Erkennung
    first_seen_at       TEXT    NOT NULL,
    last_seen_at        TEXT    NOT NULL,
    raw_path            TEXT,

    UNIQUE (portal, source_id)
);

CREATE INDEX idx_jobs_status      ON jobs (status);
CREATE INDEX idx_jobs_posted      ON jobs (posted_at DESC);
CREATE INDEX idx_jobs_canton      ON jobs (canton);
CREATE INDEX idx_jobs_cluster     ON jobs (cluster_id);
CREATE INDEX idx_jobs_company     ON jobs (company_name);
CREATE INDEX idx_jobs_agency      ON jobs (company_is_agency);

-- Ein Lauf des Fetchers. Macht Inkremente nachvollziehbar und erlaubt,
-- nach einem Abbruch dort weiterzumachen, wo es aufgehört hat.
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY,
    source          TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running',  -- running | ok | failed
    params          TEXT    NOT NULL DEFAULT '{}',       -- JSON
    items_seen      INTEGER NOT NULL DEFAULT 0,
    items_new       INTEGER NOT NULL DEFAULT 0,
    items_updated   INTEGER NOT NULL DEFAULT 0,
    requests_made   INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

CREATE INDEX idx_runs_source ON runs (source, started_at DESC);

-- Berufe-Taxonomie (aus dem DSP-Vorprojekt, berufsberatung.ch).
-- Liefert lesbare Cluster-Labels und deutschsprachiges Skill-Vokabular.
CREATE TABLE berufe (
    job_id              TEXT PRIMARY KEY,   -- berufsberatung job_id
    title               TEXT NOT NULL,
    berufsfeld          TEXT,
    branchen            TEXT,
    swissdoc            TEXT,
    bildungstyp         TEXT,
    description         TEXT,
    taetigkeiten        TEXT NOT NULL DEFAULT '{}',  -- JSON: {Kategorie: [Tätigkeit,…]}
    verwandte_berufe    TEXT NOT NULL DEFAULT '[]',  -- JSON-Array
    career_progression  TEXT NOT NULL DEFAULT '[]'   -- JSON-Array
);

CREATE INDEX idx_berufe_feld  ON berufe (berufsfeld);
CREATE INDEX idx_berufe_title ON berufe (title);

-- PLZ -> Koordinaten. job-room liefert Koordinaten mit, die CH-Media-Portale
-- nur Ort und PLZ; für Radius-Scoring brauchen wir sie überall.
CREATE TABLE plz_coords (
    postal_code TEXT NOT NULL,
    city        TEXT NOT NULL,
    canton      TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    PRIMARY KEY (postal_code, city)
);

CREATE INDEX idx_plz_code ON plz_coords (postal_code);
"""


_MIGRATION_2 = """
-- Bekannte Inserats-IDs pro Portal, unabhängig davon, ob die Detailseite
-- schon geholt wurde.
--
-- Hintergrund: die CH-Media-Sitemaps führen 53'000 (myjob) bzw. 6'100
-- (ostjob) Inserate. Alle Detailseiten zu holen wäre bei 1 req/s ein
-- 15-Stunden-Crawl — unverhältnismässig gegenüber dem Betreiber und
-- unnötig, weil sich täglich nur ein Bruchteil ändert.
--
-- `lastmod` taugt nicht als Änderungssignal (bei myjob tragen 44'000
-- Einträge dasselbe Datum aus einer Massen-Regenerierung). Die IDs sind
-- dagegen aufsteigend vergeben und die Sitemap ist absteigend sortiert:
-- neue Inserate stehen oben. Diese Tabelle merkt sich, was wir kennen,
-- damit nur wirklich Neues geholt wird.
CREATE TABLE source_items (
    portal       TEXT    NOT NULL,
    source_id    TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    lastmod      TEXT,
    first_seen_at TEXT   NOT NULL,
    fetched_at   TEXT,               -- NULL = Detailseite noch nicht geholt
    http_status  INTEGER,            -- 410 = Inserat abgelaufen
    raw_path     TEXT,
    PRIMARY KEY (portal, source_id)
);

CREATE INDEX idx_source_items_pending ON source_items (portal, fetched_at);
"""


_MIGRATION_3 = """
-- Gruppe mehrfach ausgeschriebener Stellen derselben Firma.
--
-- Alle Zeilen bleiben erhalten, denn für den Radiusfilter zählt der einzelne
-- Ort: dieselbe Stelle wird in 48 Gemeinden ausgeschrieben, relevant ist die
-- eine in Reichweite. Der Digest zeigt pro Gruppe nur den passendsten Treffer.
ALTER TABLE jobs ADD COLUMN group_id TEXT;
ALTER TABLE jobs ADD COLUMN group_size INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jobs ADD COLUMN is_group_representative INTEGER NOT NULL DEFAULT 1;

CREATE INDEX idx_jobs_group ON jobs (group_id);
"""


_MIGRATION_4 = """
-- Embedding-Cache. Schlüssel ist ein Hash aus Modell, Dimension, Task-Typ
-- und Text: dasselbe Inserat wird nie zweimal eingebettet, auch nicht über
-- mehrere Suchprofile hinweg. Der Vektor liegt als float32-Blob.
CREATE TABLE embeddings (
    cache_key TEXT PRIMARY KEY,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vector    BLOB NOT NULL
);

-- Ein Score pro (Profil, Inserat). Alle Teilscores werden mitgespeichert,
-- damit im Digest begründbar ist, warum ein Job oben oder unten steht —
-- und damit die Gewichte ab M6 gegen echte Bewertungen kalibrierbar sind.
CREATE TABLE scores (
    profile          TEXT NOT NULL,
    job_rowid        INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    final_score      REAL NOT NULL,
    semantic_raw     REAL,          -- rohe Cosine, zur Diagnose
    semantic_spread  REAL,          -- rangnormalisiert im Kandidatenpool
    best_role        TEXT,          -- welche Zielrolle am besten passte
    rules_json       TEXT NOT NULL DEFAULT '{}',
    distance_km      REAL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY (profile, job_rowid)
);

CREATE INDEX idx_scores_rank ON scores (profile, final_score DESC);
"""


_MIGRATION_5 = """
-- Begründung der Vermittler-Einstufung. Nachvollziehbar statt magisch:
-- die Selbstdeklaration `surrogate` ist unzuverlässig, deshalb entscheidet
-- ein kombiniertes Signal (siehe jobpipe.parse.agency).
ALTER TABLE jobs ADD COLUMN agency_reason TEXT NOT NULL DEFAULT '';
"""


_MIGRATION_6 = """
-- Was einem Profil im Digest schon einmal gezeigt wurde.
--
-- Grundlage für "neu seit dem letzten Digest". Bewusst nicht über
-- `jobs.first_seen_at` gelöst: ein Inserat kann seit Tagen im Bestand liegen
-- und erst heute durch geänderte Gewichte oder neue Konkurrenz in die Top-N
-- rutschen. Neu ist, was DU noch nicht gesehen hast.
CREATE TABLE digest_seen (
    profile        TEXT NOT NULL,
    job_rowid      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    first_shown_at TEXT NOT NULL,
    PRIMARY KEY (profile, job_rowid)
);

-- Ein erzeugter Digest. Macht nachvollziehbar, was wann ausgeliefert wurde.
CREATE TABLE digest_runs (
    id         INTEGER PRIMARY KEY,
    profile    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    shown      INTEGER NOT NULL DEFAULT 0,
    new_count  INTEGER NOT NULL DEFAULT 0,
    path       TEXT
);

CREATE INDEX idx_digest_runs ON digest_runs (profile, created_at DESC);
"""


_MIGRATION_7 = """
-- Deine Bewertung eines Treffers. Grundlage für die Kalibrierung der
-- Gewichte in M6.
--
-- `reason` ist der Grund einer Ablehnung aus einer festen Liste. Ein blosses
-- Nein sagt nur, dass etwas nicht passte — erst der Grund verrät, WELCHES
-- Gewicht daneben liegt. "zu weit weg" zieht am Ortsgewicht, "falscher Beruf"
-- am semantischen Anteil.
CREATE TABLE feedback (
    profile     TEXT NOT NULL,
    job_rowid   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    rating      INTEGER NOT NULL,   -- 1 interessant | 0 vielleicht | -1 nein
    reason      TEXT,               -- nur bei Ablehnung
    note        TEXT,
    score_seen  REAL,               -- Score zum Zeitpunkt der Bewertung
    created_at  TEXT NOT NULL,
    PRIMARY KEY (profile, job_rowid)
);

CREATE INDEX idx_feedback_rating ON feedback (profile, rating);
"""


_MIGRATION_8 = """
-- Job-Typ-Cluster. Profil-unabhängig: die Landschaft des Stellenmarkts ist
-- dieselbe, egal wer sucht.
CREATE TABLE clusters (
    label      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    terms      TEXT NOT NULL DEFAULT '[]',  -- JSON: charakteristische Begriffe
    beruf      TEXT,        -- ähnlichster Beruf aus berufe_ch.json
    berufsfeld TEXT,        -- dessen offizielles Berufsfeld
    similarity REAL
);

CREATE TABLE job_clusters (
    job_rowid     INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cluster_label INTEGER NOT NULL,
    cluster_name  TEXT
);

CREATE INDEX idx_job_clusters ON job_clusters (cluster_label);
"""


_MIGRATION_9 = """
-- Am Text erkannte Sprache des Inserats.
--
-- Nötig, weil das Etikett der Quelle lügt: ein STIHL-Inserat für Wil SG trägt
-- languageIsoCode = 'de' und ist durchgehend französisch geschrieben.
-- Gemessen sind 2.5% des Bestands französisch, im kaufmännischen Pool 7.1%.
ALTER TABLE jobs ADD COLUMN description_language TEXT;

CREATE INDEX idx_jobs_language ON jobs (description_language);
"""


def migrate(conn: sqlite3.Connection) -> int:
    """Hebt das Schema auf ``SCHEMA_VERSION``. Gibt die neue Version zurück."""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return current

    if current < 1:
        conn.executescript(_MIGRATION_1)
        current = 1
    if current < 2:
        conn.executescript(_MIGRATION_2)
        current = 2
    if current < 3:
        conn.executescript(_MIGRATION_3)
        current = 3
    if current < 4:
        conn.executescript(_MIGRATION_4)
        current = 4
    if current < 5:
        conn.executescript(_MIGRATION_5)
        current = 5
    if current < 6:
        conn.executescript(_MIGRATION_6)
        current = 6
    if current < 7:
        conn.executescript(_MIGRATION_7)
        current = 7
    if current < 8:
        conn.executescript(_MIGRATION_8)
        current = 8
    if current < 9:
        conn.executescript(_MIGRATION_9)
        current = 9

    conn.execute(f"PRAGMA user_version = {current}")
    return current


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


# --------------------------------------------------------------------------
# Run-Tracking
# --------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, source: str, params: dict[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO runs (source, started_at, params) VALUES (?, ?, ?)",
        (source, _now(), json.dumps(params, ensure_ascii=False, default=str)),
    )
    return int(cur.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str = "ok",
    items_seen: int = 0,
    items_new: int = 0,
    items_updated: int = 0,
    requests_made: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE runs
              SET finished_at = ?, status = ?, items_seen = ?, items_new = ?,
                  items_updated = ?, requests_made = ?, error = ?
            WHERE id = ?""",
        (_now(), status, items_seen, items_new, items_updated, requests_made, error, run_id),
    )


def last_successful_run(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        """SELECT * FROM runs
            WHERE source = ? AND status = 'ok'
         ORDER BY started_at DESC LIMIT 1""",
        (source,),
    ).fetchone()
    return row


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
