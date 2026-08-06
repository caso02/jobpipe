"""Vermittler-Erkennung.

Alle Schwellen sind an echten Firmen aus dem Bestand kalibriert. Die
Testfälle sind darum keine erfundenen Beispiele, sondern die tatsächlich
gemessenen Kennzahlen — sie halten die Kalibrierung fest.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobpipe.parse import agency
from jobpipe.parse.agency import (
    CompanyProfile,
    annotate,
    build_profiles,
    classify,
    is_public_employer,
    matches_pattern,
    name_suggests_agency,
)
from jobpipe.parse.schema import JobPosting


def profile(
    name: str,
    ads: int,
    codes: int,
    cities: int,
    spread_km: float = 0.0,
    declared: bool = False,
    client_mentions: int = 5,
) -> CompanyProfile:
    """Baut ein Firmenprofil mit den gewünschten Kennzahlen.

    Die Punkte werden so gesetzt, dass die mittlere Entfernung vom Schwerpunkt
    ungefähr ``spread_km`` beträgt: zwei Punkte im Abstand von 2*spread.
    """
    deg = spread_km / 111.0
    points = [(47.5 - deg, 8.9), (47.5 + deg, 8.9)] if spread_km else [(47.5, 8.9)] * 2
    return CompanyProfile(
        name=name,
        ads=ads,
        codes={f"c{i}" for i in range(codes)},
        cities={f"Ort{i}" for i in range(cities)},
        points=points,
        declared_agency=declared,
        client_mentions=client_mentions,
    )


class TestNameHeuristics:
    @pytest.mark.parametrize(
        "name",
        [
            "MediPersonal",
            "iPersonal AG",
            "Alegro AG Personal fur Bau Handwerk & Regie",
            "Excellent Personaldienstleistungen AG",
            "Wetzel Personalberatung AG",
            "Randstad",
            "job impuls AG",
            "Yellowshark",
            "Work Selection",
            "Planova Human Capital AG",
            "Stutz & Partner Personaldienstleistungen",
        ],
    )
    def test_known_agencies_by_name(self, name: str) -> None:
        assert name_suggests_agency(name)

    @pytest.mark.parametrize(
        "name",
        ["Stadler Rail AG", "Kistler AG", "AXA Versicherungen AG", "medmix Switzerland AG"],
    )
    def test_real_employers_not_matched_by_name(self, name: str) -> None:
        assert not name_suggests_agency(name)

    @pytest.mark.parametrize(
        "name",
        [
            "Kanton Zürich",
            "Kantonale Verwaltung Zürich",
            "Stadt Zurich",
            "Post CH AG",
            "Coop Genossenschaft",
            "Klinik Hirslanden",
            "Universitätsspital Zürich",
        ],
    )
    def test_public_employers_recognised(self, name: str) -> None:
        assert is_public_employer(name)


class TestPattern:
    def test_agency_pattern_matches(self) -> None:
        """Yellowshark: 20 Codes bei 32 Inseraten, 17 Orte, 23 km Streuung."""
        assert matches_pattern(profile("Yellowshark", 32, 20, 17, 23.1))

    def test_wide_spread_without_many_cities(self) -> None:
        """Trabeco AG: 38 Codes bei 60 Inseraten, nur 4 Orte, aber 9 km Streuung."""
        assert matches_pattern(profile("Trabeco AG", 60, 38, 4, 8.8))

    def test_public_employer_pattern_would_match_but_name_saves_it(self) -> None:
        """Kanton Zürich hat 21 Codes bei 31 Inseraten — aber nur einen Ort.

        Selbst wenn das Muster zöge, entscheidet die Prüfung auf öffentliche
        Arbeitgeber vorher.
        """
        kanton = profile("Kanton Zürich", 31, 21, 1, 0.0)
        assert not matches_pattern(kanton)
        assert classify(kanton).is_agency is False

    def test_small_sample_is_not_judged_by_pattern(self) -> None:
        """Regression: Stadler Rail und Raiffeisen wurden fälschlich markiert.

        Stadler Rail: 15 Codes bei 19 Inseraten, 6 Orte, 22 km Streuung.
        Raiffeisen Schweiz: 11 Codes bei 17 Inseraten, 10 Orte, 29 km.
        Beide sind Arbeitgeber mit echten, verteilten Standorten. Unter rund
        25 Inseraten ist die Stichprobe zu klein für ein Urteil.
        """
        assert not matches_pattern(profile("Stadler Rail AG", 19, 15, 6, 22.0))
        assert not matches_pattern(profile("Raiffeisen Schweiz", 17, 11, 10, 29.0))

    def test_low_code_diversity_is_not_enough(self) -> None:
        """Ein Filialist schreibt an vielen Orten aus, aber denselben Beruf."""
        assert not matches_pattern(profile("Volg Detailhandels AG", 40, 2, 30, 25.0))


class TestClassify:
    def test_precedence_public_beats_pattern(self) -> None:
        p = profile("Kantonsspital", 100, 60, 40, 30.0)
        v = classify(p)
        assert not v.is_agency
        assert "öffentlich" in v.reason

    def test_self_declaration_still_counts(self) -> None:
        """`surrogate = True` ist verlässlich, wenn es gesetzt ist.

        Unzuverlässig ist nur das Fehlen: Yellowshark und Randstad setzen
        ausdrücklich False.
        """
        v = classify(profile("Irgendeine AG", 5, 1, 1, declared=True))
        assert v.is_agency
        assert "surrogate" in v.reason

    def test_ordinary_employer_stays_clean(self) -> None:
        v = classify(profile("Bäckerei Müller AG", 3, 2, 1))
        assert not v.is_agency
        assert v.reason == ""

    def test_reason_is_recorded_for_audit(self) -> None:
        v = classify(profile("Yellowshark", 32, 20, 17, 23.1))
        assert v.is_agency
        assert v.reason  # nie leer, wenn als Vermittler eingestuft


class TestAnnotate:
    def _job(self, company: str, **kw: Any) -> JobPosting:
        base: dict[str, Any] = {
            "portal": "job_room",
            "source_id": kw.pop("sid", "1"),
            "source_url": "https://x",
            "title": "Titel",
            "company_name": company,
        }
        return JobPosting(**(base | kw))

    def test_sets_flag_and_reason_on_all_ads(self) -> None:
        jobs = [
            self._job("Muster Personal AG", sid=str(i), city=f"Ort{i}", lat=47.5, lon=8.9)
            for i in range(3)
        ]
        verdicts = annotate(jobs)
        assert all(j.company_is_agency for j in jobs)
        assert all(j.agency_reason == "Firmenname" for j in jobs)
        assert len(verdicts) == 1

    def test_overrides_wrong_self_declaration(self) -> None:
        """Der eigentliche Zweck des Moduls.

        Yellowshark setzt `surrogate = False`. Vorher blieb der Malus deshalb
        wirkungslos; jetzt entscheidet der Firmenname.
        """
        jobs = [
            self._job("Yellowshark", sid=str(i), company_is_agency=False, lat=47.5, lon=8.9)
            for i in range(3)
        ]
        annotate(jobs)
        assert all(j.company_is_agency for j in jobs)

    def test_groups_by_normalised_company_name(self) -> None:
        """ "Stadler Rail AG" und "Stadler Rail" sind dieselbe Firma."""
        jobs = [
            self._job("Stadler Rail AG", sid="1", lat=47.5, lon=8.9),
            self._job("Stadler Rail", sid="2", lat=47.6, lon=9.0),
        ]
        assert len(build_profiles(jobs)) == 1

    def test_real_employer_is_not_flagged(self) -> None:
        jobs = [self._job("Kistler AG", sid=str(i), lat=47.5, lon=8.9) for i in range(5)]
        annotate(jobs)
        assert not any(j.company_is_agency for j in jobs)


class TestClientMention:
    """Der Test, der Grossbetriebe von Vermittlern trennt.

    Berufsvielfalt und Ortsstreuung allein reichen nicht: *Stadler Rail
    Management AG* (0.42 Codes je Inserat, 6 Orte) und *Raiffeisen Schweiz*
    (0.50, 10 Orte) liegen mitten im Wertebereich echter Vermittler
    (0.03 bis 0.83). Beide wurden deshalb falsch etikettiert. Zuverlässig ist
    erst, dass eine Vermittlung ihre Auftraggeberfirma im Text benennt.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Für unseren Kunden in Winterthur suchen wir eine Sachbearbeiterin.",
            "Für einen langjährigen Kunden suchen wir per sofort",
            "Im Auftrag unseres Auftraggebers besetzen wir diese Position",
            "iPersonal vermittelt diese Stelle für ein Unternehmen in Pfäffikon",
            "Unsere Kundin ist ein führendes Industrieunternehmen",
        ],
    )
    def test_client_wording_recognised(self, text: str) -> None:
        assert agency.CLIENT_MENTION_RE.search(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Wir sind für unsere Kundinnen und Kunden da.",
            "Du betreust unsere Kunden am Telefon und per E-Mail.",
            "Unsere Kunden schätzen die persönliche Beratung.",
        ],
    )
    def test_service_language_is_not_a_client_mention(self, text: str) -> None:
        """Regression: mit dem lockeren Muster lag Post CH AG bei 30 % statt 0 %.

        "Für unsere Kunden" ist Dienstleistungssprache und steht bei der Post
        so gut wie bei jeder Bank.
        """
        assert not agency.CLIENT_MENTION_RE.search(text)

    def test_pattern_needs_client_mentions(self) -> None:
        """Ohne Nennung einer Auftraggeberfirma zieht das Muster nicht."""
        gross = profile("Stadler Rail Management AG", 26, 11, 6, 11.0, client_mentions=0)
        assert not matches_pattern(gross)
        assert classify(gross).is_agency is False

    def test_pattern_still_catches_real_agencies(self) -> None:
        vermittler = profile("Trabeco AG", 60, 38, 4, 8.8, client_mentions=12)
        assert matches_pattern(vermittler)
