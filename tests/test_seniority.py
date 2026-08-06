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

    @pytest.mark.parametrize(
        ("text", "erwartet"),
        [
            ("Mindestens zwei Jahre Erfahrung in der HR Administration.", 2),
            ("Wir erwarten fünf Jahre Berufserfahrung.", 5),
            ("Ein Jahr Erfahrung genügt.", 1),
            ("Mindestens zehn Jahre Berufserfahrung.", 10),
        ],
    )
    def test_spelled_out_years(self, text: str, erwartet: int) -> None:
        """Regression: die erste Fassung verlangte Ziffern.

        Gemessen nennen 356 Inserate die Berufsjahre als Ziffer und 79 als
        ausgeschriebenes Wort. Darunter war der höchstplatzierte Treffer des
        Profils, für den ``detect()`` deshalb gar nichts fand.
        """
        assert sen.detect("Sachbearbeiter", text).years == erwartet


class TestFurtherEducation:
    """Weiterbildung ohne erkennbare Stufe.

    ``EDUCATION_PATTERNS`` kennt nur HF, FH, Uni und Fachausweis. Eine
    "Weiterbildung als HR Sachbearbeiter:in" ist keine davon und war deshalb
    unsichtbar — obwohl genau sie die Hürde für eine Berufseinsteigerin ist.
    """

    def test_required_further_education_is_detected(self) -> None:
        req = sen.detect(
            "Sachbearbeiter:in Human Resources",
            "Kaufmännische Ausbildung mit Weiterbildung als HR Sachbearbeiter:in",
        )
        assert req.further_education
        assert not req.further_education_is_soft

    def test_soft_further_education(self) -> None:
        req = sen.detect("Sachbearbeiterin", "Weiterbildung als Berufsbildnerin von Vorteil")
        assert req.further_education
        assert req.further_education_is_soft

    @pytest.mark.parametrize(
        "text",
        [
            "Bereitschaft für Weiterbildung zum Wasserwart",
            "Karriere mit Perspektive – Weiterbildung zum Automobil-Mechatroniker",
            "Weiterbildung als Berufsbildner/in oder Bereitschaft, diese zu absolvieren",
            "Wir bieten Weiterbildung zur Fachfrau Finanzen",
        ],
    )
    def test_offered_further_education_is_no_hurdle(self, text: str) -> None:
        """Ein Angebot ist keine Bedingung.

        Alle vier Wortlaute stammen aus echten Inseraten. Wer die Weiterbildung
        erst mitbringen soll, ist ausgeschlossen; wer sie bekommt, nicht.
        """
        assert sen.detect("X", text).further_education is None

    def test_known_level_takes_precedence(self) -> None:
        """ "Weiterbildung zum Techniker HF" ist bereits über HF erfasst."""
        req = sen.detect("X", "Weiterbildung zum Techniker HF erforderlich")
        assert req.education == "hf"
        assert req.further_education is None

    def test_penalty_only_below_own_level(self) -> None:
        req = sen.Requirements(further_education="Weiterbildung als HR Sachbearbeiterin")
        assert sen.penalty(req, own_education="efz")[0] > 0
        assert sen.penalty(req, own_education="fh")[0] == 0.0

    def test_soft_weighs_less(self) -> None:
        hart = sen.Requirements(further_education="x")
        weich = sen.Requirements(further_education="x", further_education_is_soft=True)
        assert (
            sen.penalty(weich, own_education="efz")[0] < sen.penalty(hart, own_education="efz")[0]
        )

    def test_entry_friendly_detected(self) -> None:
        req = sen.detect("Sachbearbeiter", "Auch Berufseinsteigende sind willkommen.")
        assert req.is_entry_friendly


class TestLanguages:
    """Echte Fälle aus ihren Ablehnungen.

    Der wiederkehrende Grund war "zwingend": das Inserat sagt ausdrücklich,
    dass ohne diese Sprache nichts geht. Eine Sprache lässt sich anders als
    fehlende Berufsjahre nicht im Anschreiben ausgleichen.
    """

    def test_mandatory_language_detected(self) -> None:
        req = sen.detect(
            "Sachbearbeiter Leistungen Ausland",
            "Du verfügst zwingend über sehr gute Französisch- und Italienischkenntnisse.",
        )
        assert req.languages["fr"] == sen.LANG_HARD
        assert req.languages["it"] == sen.LANG_HARD

    def test_soft_language_is_free(self) -> None:
        """ "Französisch von Vorteil" steht in sehr vielen Inseraten."""
        req = sen.detect("Sachbearbeiterin", "Französischkenntnisse sind von Vorteil.")
        assert req.languages["fr"] == sen.LANG_SOFT

    def test_working_language_without_marker(self) -> None:
        """Die grosse Lücke: 164 von 181 Fällen trugen gar kein Markerwort.

        Wortlaut aus einem echten Inserat. Ohne Einschränkung genannt, also
        Arbeitssprache — wer kein Französisch kann, erfüllt die Aufgabe nicht.
        """
        req = sen.detect(
            "Sachbearbeiter Verkauf Innendienst",
            "Selbstständige Kundenbetreuung in den Sprachen Deutsch, Französisch und Englisch",
        )
        assert req.languages["fr"] == sen.LANG_PROBABLE

    def test_soft_marker_does_not_leak_across_segments(self) -> None:
        """Regression: der Geltungsbereich endet am Zeilen- oder Satzende.

        Ohne Begrenzung machte das "von Vorteil" der zweiten Zeile die erste
        Zeile mit weich — und umgekehrt färbte ein "zwingend" auf eine
        harmlose Nachbarzeile ab.
        """
        req = sen.detect(
            "Sachbearbeiterin",
            "Kundenkorrespondenz auf Französisch\nItalienischkenntnisse sind von Vorteil",
        )
        assert req.languages["fr"] == sen.LANG_PROBABLE
        assert req.languages["it"] == sen.LANG_SOFT

    def test_axa_wording_stays_soft(self) -> None:
        """Echter Fall: die Stelle, die sie selbst als passend bewertet hat.

        Würde die Regel hier anschlagen, verlöre sie ihren besten Treffer.
        """
        req = sen.detect(
            "Sachbearbeiter:in Schaden Motorfahrzeuge",
            "im Idealfall verfügst du über Französisch-, Englisch- und/oder Italienischkenntnisse",
        )
        assert req.languages["fr"] == sen.LANG_SOFT

    def test_trailing_clause_does_not_soften(self) -> None:
        """Der Fall, der die Sanitas-Stelle acht Ränge zu hoch stehen liess.

        Das "von Vorteil" bezieht sich auf "weitere Sprachen", nicht auf
        Italienisch. Ohne Komma-Grenze las die Regel die ganze Zeile als weich
        und die Stelle blieb trotz zwingender Sprachanforderung auf Rang 7.
        """
        req = sen.detect(
            "Sachbearbeiter Leistungen Ausland",
            "Du verfügst zwingend über sehr gute Italienischkenntnisse, "
            "weitere Sprachen sind von Vorteil",
        )
        assert req.languages["it"] == sen.LANG_HARD

    def test_missing_mandatory_language_is_a_hard_stop(self) -> None:
        req = sen.Requirements(languages={"fr": sen.LANG_HARD})
        value, why = sen.penalty(req, own_languages=("de", "en"))
        assert value == 1.0
        assert "FR" in why

    def test_working_language_costs_less_than_mandatory(self) -> None:
        hart = sen.penalty(sen.Requirements(languages={"fr": sen.LANG_HARD}), own_languages=("de",))
        wahrsch = sen.penalty(
            sen.Requirements(languages={"fr": sen.LANG_PROBABLE}), own_languages=("de",)
        )
        assert 0.0 < wahrsch[0] < hart[0]

    def test_language_one_has_is_free(self) -> None:
        req = sen.Requirements(languages={"fr": sen.LANG_HARD, "en": sen.LANG_HARD})
        assert sen.penalty(req, own_languages=("de", "en", "fr"))[0] == 0.0

    def test_soft_language_never_penalises(self) -> None:
        req = sen.Requirements(languages={"fr": sen.LANG_SOFT})
        assert sen.penalty(req, own_languages=("de",))[0] == 0.0


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
