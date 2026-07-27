"""Anforderungsniveau.

Alle Fälle stammen aus echten Treffern, die für eine Berufseinsteigerin ein
Jahr nach dem KV-EFZ ganz oben standen, obwohl sie unerreichbar sind.
"""

from __future__ import annotations

import pytest

from jobpipe.enrich import seniority as sen


class TestLeadershipDetection:
    @pytest.mark.parametrize(
        "title",
        [
            "Teamleiter:in Administration Sicherheitssysteme 80-100%",
            "Teamleader ICT Finance & HR Applications 80-100%",
            "Mandatsleiter/in Treuhand",
            "Abteilungsleiter Finanzen",
            "Gruppenleiter Records Management",
            "Head of Operations",
            "Team Lead Finance",
            "Geschäftsführer:in",
        ],
    )
    def test_leadership_titles(self, title: str) -> None:
        assert sen.detect(title, "").is_leadership, title

    @pytest.mark.parametrize(
        "title",
        [
            "Sachbearbeiter:in Human Resources 80 - 100%",
            "Sachbearbeiter/in Logistik 60%-100%",
            "Sachbearbeitung Personenschaden UVG / KTG",
            "Kaufmännische Angestellte 80-100%",
        ],
    )
    def test_plain_roles_are_not_leadership(self, title: str) -> None:
        assert not sen.detect(title, "").is_leadership, title

    def test_compounds_are_caught(self) -> None:
        """Regression: die erste Fassung verlangte eine Wortgrenze vor dem Stamm.

        Damit fielen "Abteilungsleiter" und "Teamleader" durch — derselbe
        Fehler wie zuvor bei "Kantonsspital" in der Vermittler-Erkennung.
        """
        assert sen.detect("Abteilungsleiter Finanzen", "").is_leadership
        assert sen.detect("Teamleader ICT", "").is_leadership

    def test_leadership_in_body_is_ignored(self) -> None:
        """Nur der Titel zählt.

        Gemessen enthielten 21 von 50 Inseraten irgendwo "Führung" oder
        "Leitung" — fast alle harmlos ("in einem Führungsteam", "unter der
        Leitung von").
        """
        req = sen.detect(
            "Sachbearbeiterin Administration",
            "Du arbeitest in einem Führungsteam unter der Leitung des CFO.",
        )
        assert not req.is_leadership


class TestSeniorDetection:
    @pytest.mark.parametrize(
        "title",
        [
            "Fachspezialist:in Prozessmanagement",
            "Payroll Spezialist Abacus, 80%",
            "Programm- & Projektmanager/in 80-100%",
            "Senior Sachbearbeiter Finanzen",
        ],
    )
    def test_senior_titles(self, title: str) -> None:
        assert sen.detect(title, "").is_senior, title

    def test_junior_overrides(self) -> None:
        """ "Junior Fachspezialist" ist ausdrücklich für den Einstieg gedacht."""
        assert not sen.detect("Junior Fachspezialist Prozesse", "").is_senior
        assert not sen.detect("Trainee Spezialist Finanzen", "").is_senior

    def test_projektmanager_compound(self) -> None:
        """Regression: "Projektmanager" wurde ohne Kompositum-Unterstützung übersehen."""
        assert sen.detect("Programm- & Projektmanager/in", "").is_senior


class TestEducation:
    def test_hard_requirement(self) -> None:
        req = sen.detect("Sachbearbeiter", "Abgeschlossene höhere Ausbildung (HF/FH/Uni) nötig.")
        assert req.education_rank >= sen.EDUCATION_RANK["hf"]
        assert not req.education_is_soft

    def test_soft_requirement_is_marked(self) -> None:
        """ "HF von Vorteil" ist keine Absage."""
        req = sen.detect("Sachbearbeiter", "Weiterbildung HF von Vorteil, aber nicht Bedingung.")
        assert req.education_is_soft

    def test_no_requirement(self) -> None:
        req = sen.detect("Sachbearbeiter", "Du bringst eine kaufmännische Grundbildung mit.")
        assert req.education == "keine"

    def test_years_extracted(self) -> None:
        req = sen.detect("Sachbearbeiter", "Mindestens 5 Jahre Berufserfahrung erforderlich.")
        assert req.years == 5

    def test_entry_friendly_detected(self) -> None:
        req = sen.detect("Sachbearbeiter", "Auch Berufseinsteigende sind willkommen.")
        assert req.is_entry_friendly


class TestPenalty:
    def test_leadership_fully_penalised_when_rejected(self) -> None:
        """Wer Führung ausdrücklich ablehnt, meint das auch.

        Eine Teilstrafe schob den Treffer nur ein paar Plätze nach unten —
        "Teamleiter:in" stand danach immer noch auf Rang 7.
        """
        req = sen.Requirements(is_leadership=True)
        value, why = sen.penalty(req, accept_leadership=False)
        assert value == 1.0
        assert "Führung" in why

    def test_no_penalty_when_leadership_accepted(self) -> None:
        req = sen.Requirements(is_leadership=True)
        assert sen.penalty(req, accept_leadership=True)[0] == 0.0

    def test_education_gap_scales(self) -> None:
        """Je grösser der Abstand zum eigenen Abschluss, desto höher die Hürde."""
        efz_vs_hf = sen.penalty(sen.Requirements(education="hf"), own_education="efz")[0]
        efz_vs_uni = sen.penalty(sen.Requirements(education="uni"), own_education="efz")[0]
        assert efz_vs_uni > efz_vs_hf > 0

    def test_soft_education_weighs_less(self) -> None:
        hard = sen.penalty(sen.Requirements(education="hf"), own_education="efz")[0]
        soft = sen.penalty(
            sen.Requirements(education="hf", education_is_soft=True), own_education="efz"
        )[0]
        assert soft < hard

    def test_own_level_matches_no_penalty(self) -> None:
        assert sen.penalty(sen.Requirements(education="efz"), own_education="efz")[0] == 0.0

    def test_higher_own_education_no_penalty(self) -> None:
        assert sen.penalty(sen.Requirements(education="efz"), own_education="fh")[0] == 0.0

    def test_years_within_tolerance(self) -> None:
        req = sen.Requirements(years=3)
        assert sen.penalty(req, own_years=1, tolerance_years=2)[0] == 0.0
        assert sen.penalty(req, own_years=1, tolerance_years=0)[0] > 0.0

    def test_entry_friendly_halves_the_hurdle(self) -> None:
        """Ausdrücklich geöffnete Stellen sind erreichbarer, auch wenn sie fordern."""
        strict = sen.Requirements(education="hf")
        open_ = sen.Requirements(education="hf", is_entry_friendly=True)
        assert (
            sen.penalty(open_, own_education="efz")[0] < sen.penalty(strict, own_education="efz")[0]
        )

    def test_penalty_is_capped(self) -> None:
        req = sen.Requirements(is_leadership=True, is_senior=True, education="uni", years=15)
        value, _ = sen.penalty(
            req, own_education="efz", own_years=1, accept_leadership=False, accept_senior=False
        )
        assert value == 1.0

    def test_reason_is_human_readable(self) -> None:
        req = sen.Requirements(is_leadership=True)
        _, why = sen.penalty(req, accept_leadership=False)
        assert why and "Führungsfunktion" in why


class TestIntegrationWithRules:
    def test_rule_uses_profile_seniority(self) -> None:
        from typing import Any

        from jobpipe.config import Profile, SeniorityPreference
        from jobpipe.enrich import rules
        from jobpipe.parse.schema import JobPosting

        def job(title: str) -> Any:
            return JobPosting(
                portal="job_room",
                source_id="1",
                source_url="https://x",
                title=title,
                company_name="F",
                description_md="Text",
            )

        einstieg = Profile(
            name="p",
            display_name="P",
            seniority=SeniorityPreference(accept_leadership=False, accept_senior=False),
        )
        offen = Profile(name="p", display_name="P")

        assert rules.score_seniority(job("Teamleiter Administration"), einstieg)[0] == 1.0
        assert rules.score_seniority(job("Teamleiter Administration"), offen)[0] == 0.0
        assert rules.score_seniority(job("Sachbearbeiterin Administration"), einstieg)[0] == 0.0
