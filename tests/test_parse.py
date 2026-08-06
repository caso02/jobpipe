"""Schema, Parser, Geocoding und Deduplizierung."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobpipe.collect.ch_media import (
    PreloadedStateMissing,
    extract_preloaded_state,
    extract_vacancy,
    parse_sitemap,
)
from jobpipe.parse import ch_media as ch_media_parser
from jobpipe.parse import job_room as job_room_parser
from jobpipe.parse import lang
from jobpipe.parse.dedup import (
    MIN_TITLE_SIMILARITY,
    cluster_across_portals,
    collapse_company_duplicates,
    is_same_job,
    jaccard,
    shingles,
    title_similarity,
)
from jobpipe.parse.geo import Geocoder, haversine_km
from jobpipe.parse.schema import (
    JobPosting,
    normalize_company,
    normalize_title,
    reconcile_workload,
)
from jobpipe.store import db

FIXTURES = Path(__file__).parent / "fixtures"


def make(**kw: Any) -> JobPosting:
    base: dict[str, Any] = {
        "portal": "job_room",
        "source_id": "x",
        "source_url": "https://example.ch/1",
        "title": "Titel",
        "company_name": "Firma AG",
    }
    return JobPosting(**(base | kw))


# --------------------------------------------------------------------------


class TestNormalizeTitle:
    @pytest.mark.parametrize(
        ("raw", "city", "expected"),
        [
            ("Logistiker  100% (m/w/d)", "Landquart", "logistiker"),
            ("Logistiker*in 80-100%", "Landquart", "logistiker"),
            ("Logistiker:in", None, "logistiker"),
            ("Logistiker/-in", None, "logistiker"),
            ("Sachbearbeiter/in (w/m/d) 60%", None, "sachbearbeiter"),
            ("Mitarbeiter_innen Logistik", None, "mitarbeiter logistik"),
        ],
    )
    def test_strips_workload_and_gender_noise(
        self, raw: str, city: str | None, expected: str
    ) -> None:
        assert normalize_title(raw, city) == expected

    def test_plain_word_ending_in_in_survives(self) -> None:
        """Nur Gendermarker MIT Trennzeichen werden gekürzt.

        Ohne diese Einschränkung würde aus "Medizin" ein "Mediz".
        """
        assert "medizin" in normalize_title("Fachperson Medizin", None)

    def test_strips_location_from_title(self) -> None:
        """Massenausschreiber schreiben den Ort in den Titel.

        Ohne diesen Schritt gilt dieselbe Stelle in 48 Gemeinden als 48
        Stellen — gemessen an MediPersonal, die damit 28 % des Bestands füllten.
        """
        a = normalize_title("Fachmann Gesundheit EFZ (m/w/d) in Erlenbach gesucht", "Erlenbach")
        b = normalize_title("Fachmann Gesundheit EFZ (m/w/d) in Wetzikon gesucht", "Wetzikon")
        assert a == b == "fachmann gesundheit efz"

    def test_strips_marketing_tail(self) -> None:
        out = normalize_title(
            "Operationstechnik HF 80-100% für Beromünster gesucht – Spezialisiere dich auf Präzision",
            "Beromünster",
        )
        assert out == "operationstechnik hf"

    def test_keeps_hyphenated_shift_pattern(self) -> None:
        """Regression: der Marketing-Filter zerlegte "3-4 Schicht".

        Er entfernte alles nach einem Bindestrich und machte aus
        "Produktionsmitarbeiter*in 3-4 Schicht (m,w,d)" ein blosses
        "produktionsmitarbeiterin 3".
        """
        out = normalize_title("Produktionsmitarbeiter*in 3-4 Schicht (m,w,d)", "Degersheim")
        assert "schicht" in out

    def test_normalize_company_drops_legal_form(self) -> None:
        assert normalize_company("Stadler Rail AG") == normalize_company("Stadler Rail")
        assert normalize_company("VAT Group AG") == "vat"


class TestCrossPortalDedup:
    def test_same_job_on_two_portals(self) -> None:
        a = make(
            portal="job_room",
            source_id="1",
            title="Projekteinkäufer",
            company_name="Escatec Switzerland AG",
            city="Heerbrugg",
            postal_code="9435",
        )
        b = make(
            portal="ostjob",
            source_id="2",
            title="ProjekteinkäuferIn (m/w/d)",
            company_name="ESCATEC Switzerland AG",
            city="Heerbrugg",
            postal_code="9435",
        )
        matched, why = is_same_job(a, b)
        assert matched, why

    def test_different_focus_same_employer_is_not_merged(self) -> None:
        """Regression aus echten Daten.

        "Testingenieur:in mit Fokus Akustik" und "… mit Fokus EMV" bei Stadler
        Rail am selben Ort wurden zusammengeworfen: Firma (0.35) und Ort (0.20)
        brachten 0.55 mit, der Titel musste nur noch 0.71 erreichen. Seither
        muss der Titel eine eigene Hürde nehmen.
        """
        a = make(
            portal="job_room",
            source_id="1",
            title="Testingenieur:in mit Fokus Akustik 100%",
            company_name="Stadler Rail AG",
            city="St. Margrethen",
            postal_code="9430",
        )
        b = make(
            portal="ostjob",
            source_id="2",
            title="Testingenieur:in mit Fokus EMV",
            company_name="Stadler Rail Management AG",
            city="St. Margrethen",
            postal_code="9430",
        )
        matched, why = is_same_job(a, b)
        assert not matched, why

    def test_unrelated_engineering_roles_are_not_merged(self) -> None:
        a = make(
            portal="job_room",
            source_id="1",
            title="Development Engineer Sensor Protection Advanced",
            company_name="VAT Group AG",
            city="Haag",
            postal_code="9469",
        )
        b = make(
            portal="ostjob",
            source_id="2",
            title="Software Development Engineer",
            company_name="VAT Vakuumventile AG",
            city="Haag",
            postal_code="9469",
        )
        assert not is_same_job(a, b)[0]

    def test_identical_apply_url_beats_fuzzy_threshold(self) -> None:
        """Gleiche Bewerbungsseite beim Arbeitgeber ist praktisch ein Beweis."""
        url = "https://firma.example/karriere/stelle-123"
        a = make(
            portal="job_room",
            source_id="1",
            title="Ganz anderer Titel",
            company_name="A AG",
            city="Zug",
            apply_url=url,
        )
        b = make(
            portal="ostjob",
            source_id="2",
            title="Noch ein Titel",
            company_name="B GmbH",
            city="Zug",
            apply_url=url + "?utm_source=x",
        )
        matched, why = is_same_job(a, b)
        assert matched
        assert why == "apply_url"

    def test_title_gate_is_enforced(self) -> None:
        a = make(title="Koch", company_name="Restaurant AG", city="Chur")
        b = make(
            portal="ostjob",
            source_id="2",
            title="Konditor",
            company_name="Restaurant AG",
            city="Chur",
        )
        assert title_similarity(a, b) < MIN_TITLE_SIMILARITY
        assert not is_same_job(a, b)[0]

    def test_clustering_assigns_shared_id(self) -> None:
        jobs = [
            make(
                portal="job_room",
                source_id="1",
                title="Monteur/in 80-100%",
                company_name="Heliobus AG",
                city="St. Gallen",
                postal_code="9000",
            ),
            make(
                portal="ostjob",
                source_id="2",
                title="Monteur/in 80-100%",
                company_name="Heliobus AG",
                city="St.Gallen",
                postal_code="9000",
            ),
            make(
                portal="job_room",
                source_id="3",
                title="Koch",
                company_name="Beiz AG",
                city="St. Gallen",
                postal_code="9000",
            ),
        ]
        clusters = cluster_across_portals(jobs)
        multi = [c for c in clusters if len(c.portals) > 1]
        assert len(multi) == 1
        assert len(multi[0].members) == 2

    def test_canonical_prefers_job_room(self) -> None:
        jobs = [
            make(
                portal="ostjob",
                source_id="2",
                title="Monteur",
                company_name="X AG",
                postal_code="9000",
            ),
            make(
                portal="job_room",
                source_id="1",
                title="Monteur",
                company_name="X AG",
                postal_code="9000",
            ),
        ]
        cluster = cluster_across_portals(jobs)[0]
        assert cluster.canonical.portal == "job_room"


class TestPipelineDedupIntegration:
    def test_cluster_members_are_not_all_representatives(self, tmp_path: Path) -> None:
        """Beide Dedup-Dimensionen müssen zusammenwirken.

        Regression aus echten Daten: "Abteilungsleiter:in Auftragsabwicklung 3R"
        lief bei Stadler Rail AG, Stadler Service AG und Stadler Rail Management
        AG. Die Clusterung erkannte das als eine Stelle, die Firmengruppierung
        nicht — sie arbeitet pro Firmenname. Ergebnis: dreimal derselbe Treffer
        in der Liste.
        """
        from jobpipe.parse.pipeline import run

        raw_dir = tmp_path / "raw"
        (raw_dir / "job_room").mkdir(parents=True)
        import gzip

        for i, company in enumerate(
            ["Stadler Rail AG", "Stadler Service AG", "Stadler Rail Management AG"]
        ):
            payload = {
                "id": f"id-{i}",
                "jobContent": {
                    "jobDescriptions": [
                        {
                            "languageIsoCode": "de",
                            "title": "Abteilungsleiter:in Auftragsabwicklung 3R",
                            "description": "Führung der Auftragsabwicklung.",
                        }
                    ],
                    "company": {"name": company},
                    "location": {
                        "city": "Frauenfeld",
                        "postalCode": "8500",
                        "cantonCode": "TG",
                        "coordinates": {"lat": "47.556", "lon": "8.898"},
                    },
                },
            }
            with gzip.open(raw_dir / "job_room" / f"{i}.json.gz", "wb") as fh:
                fh.write(json.dumps(payload).encode())

        conn = db.init_db(tmp_path / "t.db")
        report = run(conn, raw_dir)
        assert report.parsed == 3
        shown = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_group_representative = 1"
        ).fetchone()[0]
        assert shown == 1, "dieselbe Stelle darf nur einmal angezeigt werden"
        assert conn.execute("SELECT COUNT(DISTINCT cluster_id) FROM jobs").fetchone()[0] == 1


class TestCompanyGrouping:
    def test_same_role_many_towns_collapses(self) -> None:
        """Der eigentliche Zweck: ein Vermittler, 40 Gemeinden, eine Stelle."""
        jobs = [
            make(
                source_id=str(i),
                title=f"Kranführer in {town} gesucht",
                company_name="Alegro AG",
                city=town,
                postal_code=str(8000 + i),
            )
            for i, town in enumerate(
                ["Kloten", "Regensdorf", "Dietikon", "Wallisellen", "Schlieren"]
            )
        ]
        groups = collapse_company_duplicates(jobs)
        assert len(groups) == 1
        assert groups[0].size == 5
        assert len(groups[0].locations) == 5

    def test_different_roles_stay_separate(self) -> None:
        jobs = [
            make(source_id="1", title="Kranführer", company_name="Alegro AG", city="Kloten"),
            make(
                source_id="2", title="Tiefbaufacharbeiter", company_name="Alegro AG", city="Kloten"
            ),
        ]
        assert len(collapse_company_duplicates(jobs)) == 2

    def test_different_companies_stay_separate(self) -> None:
        jobs = [
            make(source_id="1", title="Kranführer", company_name="Alegro AG", city="Kloten"),
            make(source_id="2", title="Kranführer", company_name="Baufirma GmbH", city="Kloten"),
        ]
        assert len(collapse_company_duplicates(jobs)) == 2

    def test_text_similarity_alone_would_not_work(self) -> None:
        """Warum die Gruppierung über Titel statt über Text läuft.

        Gemessen an zwei MediPersonal-Inseraten mit identischem Titel UND
        identischem Ort: Jaccard 0.27. Die Texte sind LLM-generiert und jedes
        Mal neu formuliert.
        """
        a = "Erlenbach liegt malerisch am Zürichsee und besticht durch seine idyllische Lage inmitten einer reizvollen Landschaft mit hoher Lebensqualität"
        b = "Erlenbach liegt in unmittelbarer Nähe zu Zürich und besticht durch eine ausgezeichnete Verkehrsanbindung sowie moderne Infrastruktur vor Ort"
        assert jaccard(shingles(a), shingles(b)) < 0.5


class TestJobRoomParser:
    @pytest.fixture
    def page(self) -> list[dict[str, Any]]:
        return json.loads((FIXTURES / "job_room_search_page.json").read_text(encoding="utf-8"))

    def test_parses_fixture(self, page: list[dict[str, Any]]) -> None:
        for wrapper in page:
            job = job_room_parser.parse(wrapper["jobAdvertisement"])
            assert job is not None
            assert job.portal == "job_room"
            assert job.title and job.company_name
            assert job.lat is not None and job.lon is not None  # job-room liefert Koordinaten

    def test_rejects_records_without_title(self) -> None:
        assert job_room_parser.parse({"id": "1", "jobContent": {"company": {"name": "X"}}}) is None

    def test_rejects_records_without_content(self) -> None:
        assert job_room_parser.parse({"id": "1"}) is None

    def test_agency_flag_is_taken_from_surrogate(self) -> None:
        raw = {
            "id": "1",
            "jobContent": {
                "jobDescriptions": [{"languageIsoCode": "de", "title": "T", "description": "D"}],
                "company": {"name": "Vermittler AG", "surrogate": True},
                "location": {"city": "Zürich"},
            },
        }
        job = job_room_parser.parse(raw)
        assert job is not None
        assert job.company_is_agency is True


class TestChMediaParser:
    @pytest.fixture
    def vacancies(self) -> list[dict[str, Any]]:
        return json.loads((FIXTURES / "ch_media_vacancies.json").read_text(encoding="utf-8"))

    def test_parses_fixture(self, vacancies: list[dict[str, Any]]) -> None:
        for raw in vacancies:
            job = ch_media_parser.parse(raw, "ostjob")
            assert job is not None
            assert job.portal == "ostjob"
            assert job.postal_code or job.city

    def test_iframe_ads_are_flagged_as_truncated(self, vacancies: list[dict[str, Any]]) -> None:
        """Ein Teil der Inserate bettet den Text von der Arbeitgeber-Domain ein.

        Dem iframe wird nicht gefolgt — fremde Domain, eigene robots.txt. Das
        Ranking muss aber wissen, dass der Text unvollständig ist.
        """
        with_iframe = [v for v in vacancies if v.get("url_description")]
        assert with_iframe, "Fixture enthält keinen iframe-Fall"
        job = ch_media_parser.parse(with_iframe[0], "ostjob")
        assert job is not None
        assert job.description_truncated is True

    def test_home_office_flag_is_read(self) -> None:
        raw = {"id": 1, "title": "T", "company": {"name": "X AG"}, "home_office": True}
        job = ch_media_parser.parse(raw, "myjob")
        assert job is not None
        assert job.home_office is True

    def test_agency_heuristic(self) -> None:
        assert ch_media_parser.looks_like_agency("Universal-Job AG")
        assert ch_media_parser.looks_like_agency("Meier Personal AG")
        assert not ch_media_parser.looks_like_agency("Stadler Rail AG")

    def test_workload_range(self) -> None:
        raw = {
            "id": 1,
            "title": "T",
            "company": {"name": "X AG"},
            "type_value_min": 80,
            "type_value_max": 100,
        }
        job = ch_media_parser.parse(raw, "ostjob")
        assert job is not None
        assert (job.workload_min, job.workload_max) == (80, 100)


class TestSitemapAndState:
    def test_parses_sitemap_entries(self) -> None:
        xml = """<?xml version="1.0"?><urlset>
          <url><loc>https://www.ostjob.ch/job/leitende-hebamme/1087742</loc>
               <lastmod>2026-07-27T12:07:13+02:00</lastmod></url>
          <url><loc>https://www.ostjob.ch/job/logistiker-100/1087740</loc></url>
          <url><loc>https://www.ostjob.ch/firma/xy/123</loc></url>
        </urlset>"""
        entries = parse_sitemap(xml)
        assert [e.source_id for e in entries] == ["1087742", "1087740"]
        assert entries[0].lastmod is not None
        assert entries[1].lastmod is None

    def test_extracts_nested_state(self) -> None:
        html = 'x<script>window.__PRELOADED_STATE__ = {"a":{"b":"}{"},"vacancyDetails":{"data":{"id":7}}};</script>y'
        assert extract_vacancy(html) == {"id": 7}

    def test_state_with_escaped_quotes(self) -> None:
        html = r'window.__PRELOADED_STATE__ = {"vacancyDetails":{"data":{"t":"a \" } b"}}}'
        assert extract_preloaded_state(html)["vacancyDetails"]["data"]["t"] == 'a " } b'

    def test_missing_state_is_loud(self) -> None:
        with pytest.raises(PreloadedStateMissing):
            extract_vacancy("<html>kein state</html>")

    def test_empty_data_is_loud(self) -> None:
        with pytest.raises(PreloadedStateMissing):
            extract_vacancy('window.__PRELOADED_STATE__ = {"vacancyDetails":{"data":{}}}')


class TestGeo:
    def test_haversine_zurich_bern(self) -> None:
        d = haversine_km(47.3769, 8.5417, 46.9480, 7.4474)
        assert 90 < d < 100  # Luftlinie rund 95 km

    def test_geocoder_resolves_and_enriches(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        conn.execute(
            "INSERT INTO plz_coords (postal_code, city, canton, lat, lon) VALUES (?,?,?,?,?)",
            ("8500", "Frauenfeld", "TG", 47.556, 8.898),
        )
        geo = Geocoder(conn)
        assert geo.lookup("8500", "Frauenfeld") is not None
        assert geo.lookup("8500", None) is not None
        assert geo.lookup(None, "frauenfeld") is not None
        assert geo.lookup("9999", "Nirgendwo") is None

        job = make(postal_code="8500", city="Frauenfeld")
        geo.enrich(job)
        assert job.lat == pytest.approx(47.556)
        assert job.canton == "TG"

    def test_enrich_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        conn = db.init_db(tmp_path / "t.db")
        conn.execute(
            "INSERT INTO plz_coords (postal_code, city, canton, lat, lon) VALUES (?,?,?,?,?)",
            ("8500", "Frauenfeld", "TG", 47.556, 8.898),
        )
        job = make(postal_code="8500", city="Frauenfeld", lat=1.0, lon=2.0, canton="ZH")
        Geocoder(conn).enrich(job)
        assert (job.lat, job.lon, job.canton) == (1.0, 2.0, "ZH")


class TestLanguageDetection:
    """Sprache am Text erkennen, nicht am Etikett.

    Der Auslöser: ein STIHL-Inserat für Wil SG trägt
    ``jobDescriptions[].languageIsoCode = "de"`` und ist durchgehend
    französisch geschrieben. Gemessen sind 2.5 % des Bestands französisch, im
    kaufmännischen Pool von Profil B 7.1 %.
    """

    def test_german(self) -> None:
        assert (
            lang.detect_language(
                "Wir suchen für unser Team eine Sachbearbeiterin, die uns bei der "
                "Auftragsabwicklung und in der Administration unterstützt."
            )
            == "de"
        )

    def test_french_despite_german_label(self) -> None:
        """Der echte Wortlaut aus dem Inserat, das die Regel ausgelöst hat."""
        assert (
            lang.detect_language(
                "POURQUOI STIHL. En tant qu'entreprise familiale innovante et marque "
                "mondiale leader dans le domaine des tronçonneuses, nous vous offrons "
                "un poste avec des perspectives dans une équipe qui vous soutient."
            )
            == "fr"
        )

    def test_short_text_is_unknown(self) -> None:
        """Zu wenig Material heisst «keine Aussage», nicht «keine Sprache»."""
        assert lang.detect_language("Sachbearbeiter 80%") is None
        assert lang.detect_language("") is None
        assert lang.detect_language(None) is None

    def test_german_with_english_terms_stays_german(self) -> None:
        """Deutsche Inserate zitieren ständig englische Begriffe."""
        assert (
            lang.detect_language(
                "Wir bieten dir Home Office und flexible Arbeitszeiten. Du arbeitest "
                "mit dem Team an Projekten und bist für die Administration zuständig, "
                "dabei unterstützt dich unser Customer Service Lead."
            )
            == "de"
        )


class TestLanguageFilter:
    def test_empty_wanted_keeps_everything(self) -> None:
        assert lang.matches_language("fr", [])

    def test_unknown_language_is_kept(self) -> None:
        """Im Zweifel behalten — ein Inserat wegen Unsicherheit zu verwerfen
        wäre der teurere Fehler."""
        assert lang.matches_language(None, ["de"])

    def test_wrong_language_is_dropped(self) -> None:
        assert not lang.matches_language("fr", ["de"])
        assert lang.matches_language("de", ["de"])


class TestWorkloadReconciliation:
    """Pensum im Titel gegen das strukturierte Feld.

    Auslöser: "Exportsachbearbeiter **60 %** für die Region Pfäffikon" trägt im
    Feld ``100-100``, und der Fliesstext bestätigt "eine feste Anstellung mit
    60 Prozent". Wer Vollzeit sucht, bekäme die Stelle sonst als Volltreffer.
    Gemessen betrifft das 78 von 7'473 Inseraten mit Prozentzahl im Titel.
    """

    def _job(self, titel: str, lo: int, hi: int) -> JobPosting:
        return JobPosting(
            portal="job_room",
            source_id="1",
            source_url="https://example.ch/1",
            title=titel,
            company_name="Muster AG",
            workload_min=lo,
            workload_max=hi,
        )

    def test_title_wins_on_conflict(self) -> None:
        job = self._job("Exportsachbearbeiter 60% für die Region Pfäffikon", 100, 100)
        assert reconcile_workload(job)
        assert (job.workload_min, job.workload_max) == (60, 60)
        assert job.workload_from_title

    def test_range_in_title_wins(self) -> None:
        job = self._job("Sachbearbeiter/in Administration (50-100%) 100%", 100, 100)
        assert reconcile_workload(job)
        assert (job.workload_min, job.workload_max) == (50, 100)

    @pytest.mark.parametrize(
        ("titel", "lo", "hi"),
        [
            ("Sachbearbeiter:in HR 80 - 100%", 80, 100),
            ("Fachperson Rente 80-100%", 80, 100),
            ("Mitarbeiter 100%", 100, 100),
            ("Sachbearbeiterin ohne Angabe", 80, 100),
        ],
    )
    def test_no_conflict_leaves_it_alone(self, titel: str, lo: int, hi: int) -> None:
        job = self._job(titel, lo, hi)
        assert not reconcile_workload(job)
        assert (job.workload_min, job.workload_max) == (lo, hi)
        assert not job.workload_from_title
