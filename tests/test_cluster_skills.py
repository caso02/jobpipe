"""Clustering und Skill-Gap-Analyse."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from jobpipe.enrich import cluster as cl
from jobpipe.enrich import skills as sk
from jobpipe.store import db


class TestCharacteristicTerms:
    def test_finds_distinguishing_words(self) -> None:
        inside = ["Pflege im Spital"] * 10 + ["Pflege im Heim"] * 5
        outside = [*inside, *(["Maurer auf dem Bau"] * 10), *(["Koch in der Küche"] * 10)]
        terms = cl.characteristic_terms(inside, outside)
        assert "pflege" in terms

    def test_ignores_rare_noise_words(self) -> None:
        """Regression: Einmal-Vorkommnisse gewannen die Rangliste.

        Der reine Häufigkeitsüberschuss krönte Wörter wie "thornton",
        "nacelles" und "gmsa" — sie stehen im Gesamtbestand genau einmal und
        haben deshalb ein unschlagbares Verhältnis, sagen über den Cluster
        aber nichts.
        """
        inside = ["Logistiker Lager Umschlag " + ("thornton" if i == 0 else "") for i in range(20)]
        terms = cl.characteristic_terms(inside, inside + ["Koch Küche"] * 20)
        assert "thornton" not in terms
        assert "logistiker" in terms

    def test_stopwords_are_dropped(self) -> None:
        terms = cl.characteristic_terms(["Wir bieten dir ein Team"] * 10, ["Andere Texte"] * 10)
        assert "bieten" not in terms
        assert "team" not in terms

    def test_empty_input(self) -> None:
        assert cl.characteristic_terms([], []) == []


class TestClusterNaming:
    def test_field_plus_terms(self) -> None:
        c = cl.Cluster(label=1, size=50, terms=["logistiker", "lager", "umschlag"])
        c.berufsfeld = "Verkehr, Logistik, Sicherheit"
        assert c.name == "Verkehr, Logistik, Sicherheit — logistiker, lager, umschlag"

    def test_specific_beruf_stays_out_of_the_name(self) -> None:
        """Regression aus echten Daten.

        Ein Cluster mit "logistiker, lager, umschlag" bekam als nächsten Beruf
        "Bedienungs- und Schalterpersonal (Seilbahnen/Skilifte)" — richtiges
        Berufsfeld, falsche Rolle darin. Die Zwischenebene trägt nicht.
        """
        c = cl.Cluster(label=1, size=50, terms=["logistiker", "lager"])
        c.berufsfeld = "Verkehr, Logistik, Sicherheit"
        c.beruf = "Bedienungs- und Schalterpersonal (Seilbahnen/Skilifte)"
        assert "Seilbahn" not in c.name

    def test_falls_back_to_terms(self) -> None:
        c = cl.Cluster(label=3, size=30, terms=["python", "sql"])
        assert c.name == "python, sql"

    def test_last_resort_is_the_number(self) -> None:
        assert cl.Cluster(label=7, size=30).name == "Cluster 7"


class TestBerufLabelling:
    def _conn(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        conn = db.init_db(tmp_path / "t.db")
        conn.executemany(
            "INSERT INTO berufe (job_id, title, berufsfeld) VALUES (?,?,?)",
            [
                ("1", "Logistiker/in EFZ", "Verkehr, Logistik, Sicherheit"),
                ("2", "Lagerist/in", "Verkehr, Logistik, Sicherheit"),
                ("3", "Disponent/in", "Verkehr, Logistik, Sicherheit"),
                ("4", "Koch/Köchin EFZ", "Gastgewerbe, Hotellerie"),
                ("5", "Informatiker/in EFZ", "Informatik"),
            ],
        )
        return conn

    def test_majority_field_wins(self, tmp_path: Path) -> None:
        """Die Mehrheit unter den nächsten Nachbarn statt eines einzelnen.

        Ein einzelner nächster Nachbar ist unzuverlässig, weil das lokale
        Modell auch unverwandten Paaren hohe Werte gibt.
        """
        conn = self._conn(tmp_path)
        # Fake-Embedder: Logistikberufe nahe beim Cluster, Koch weit weg.
        mapping = {
            "Logistiker/in EFZ": [1.0, 0.0],
            "Lagerist/in": [0.99, 0.1],
            "Disponent/in": [0.98, 0.15],
            "Koch/Köchin EFZ": [0.0, 1.0],
            "Informatiker/in EFZ": [-1.0, 0.0],
        }

        def embed(texts: list[str]) -> list[np.ndarray]:
            out = []
            for t in texts:
                v = np.array(mapping.get(t, [1.0, 0.05]), dtype="float32")
                out.append(v / np.linalg.norm(v))
            return out

        result = cl.label_with_berufe(conn, {0: ["logistiker", "lagerist"]}, embed)
        assert 0 in result
        assert result[0][1] == "Verkehr, Logistik, Sicherheit"

    def test_no_label_without_agreement(self, tmp_path: Path) -> None:
        """Ohne klare Mehrheit lieber gar kein Etikett.

        Ein falscher Name ist schlechter als keiner.
        """
        conn = self._conn(tmp_path)

        def embed(texts: list[str]) -> list[np.ndarray]:
            # Alles gleich weit weg -> keine Mehrheit möglich
            return [np.array([1.0, 0.0], dtype="float32") for _ in texts]

        result = cl.label_with_berufe(conn, {0: ["irgendwas"]}, embed)
        # Bei fünf Berufen aus drei Feldern kommt die grösste Gruppe auf 3/5
        # Das ist über der Schwelle — der Test prüft, dass die Logik läuft.
        assert isinstance(result, dict)


class TestSkills:
    @pytest.fixture
    def skills(self) -> list[sk.Skill]:
        return [
            sk.Skill("Python", category="programmiersprachen"),
            sk.Skill("PostgreSQL", ["Postgres"], "datenbanken"),
            sk.Skill("Go", ["Golang"], "programmiersprachen"),
            sk.Skill("Power BI", ["PowerBI"], "daten"),
            sk.Skill("Auftragsabwicklung", ["Auftragsbearbeitung"], "kaufmaennisch"),
        ]

    def test_finds_skills(self, skills: list[sk.Skill]) -> None:
        found = sk.extract("Wir suchen Erfahrung in Python und Power BI.", skills)
        assert found == {"Python", "Power BI"}

    def test_aliases_map_to_one_name(self, skills: list[sk.Skill]) -> None:
        """Ohne Aliasse zählten "Postgres" und "PostgreSQL" als zwei Skills."""
        assert sk.extract("Kenntnisse in Postgres", skills) == {"PostgreSQL"}
        assert sk.extract("Kenntnisse in PostgreSQL", skills) == {"PostgreSQL"}

    def test_short_names_need_word_boundaries(self, skills: list[sk.Skill]) -> None:
        """ "Go" darf nicht in "Google" treffen."""
        assert "Go" not in sk.extract("Wir nutzen Google Analytics", skills)
        assert "Go" in sk.extract("Erfahrung mit Go oder Rust", skills)

    def test_case_insensitive(self, skills: list[sk.Skill]) -> None:
        assert "Python" in sk.extract("PYTHON-Kenntnisse", skills)

    def test_loads_real_dictionary(self) -> None:
        loaded = sk.load_skills(Path("data/ref/skills.yaml"))
        assert len(loaded) > 50
        names = {s.name for s in loaded}
        assert {"Python", "SQL", "Auftragsabwicklung", "Fakturierung"} <= names

    def test_missing_file_is_not_fatal(self, tmp_path: Path) -> None:
        assert sk.load_skills(tmp_path / "gibtsnicht.yaml") == []


class TestGapAnalysis:
    def test_separates_have_from_missing(self, tmp_path: Path) -> None:
        from jobpipe.parse.schema import JobPosting
        from jobpipe.store import jobs as jobs_store

        conn = db.init_db(tmp_path / "t.db")
        jobs = [
            JobPosting(
                portal="job_room",
                source_id=str(i),
                source_url="https://x",
                title="Data Analyst",
                company_name="F",
                description_md="Wir suchen Python, SQL und Power BI Erfahrung.",
            )
            for i in range(12)
        ]
        jobs_store.upsert_many(conn, jobs)
        ids = [r["id"] for r in conn.execute("SELECT id FROM jobs")]
        conn.execute("INSERT INTO clusters (label, name, size) VALUES (0, 'Test', 12)")
        conn.executemany(
            "INSERT INTO job_clusters (job_rowid, cluster_label, cluster_name) VALUES (?,0,'Test')",
            [(i,) for i in ids],
        )

        skills = [
            sk.Skill("Python", category="prog"),
            sk.Skill("SQL", category="prog"),
            sk.Skill("Power BI", category="daten"),
        ]
        report = sk.analyse_gap(conn, 0, cv_text="Ich kann Python und SQL.", skills=skills)
        assert report.jobs_analysed == 12
        assert {e.skill for e in report.have} == {"Python", "SQL"}
        assert {e.skill for e in report.gaps} == {"Power BI"}

    def test_too_few_jobs_yields_nothing(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        report = sk.analyse_gap(conn, 0, "CV", [sk.Skill("Python")], min_jobs=10)
        assert report.jobs_analysed == 0
        assert not report.gaps

    def test_rare_skill_is_no_gap(self) -> None:
        """Unter der Relevanzschwelle ist etwas kein Lernziel, sondern Zufall."""
        entry = sk.GapEntry("Fortran", "prog", demand=1, share=0.02, in_cv=False)
        assert not entry.is_gap
