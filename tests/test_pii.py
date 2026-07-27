"""Der wichtigste Test des Projekts.

Wenn hier etwas durchrutscht, speichern wir Personendaten — und das ist genau
das, was laut README und PLAN.md nicht passieren darf.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from jobpipe.parse.pii import (
    EMAIL_RE,
    PHONE_RE,
    find_contact_leaks,
    redact_text,
    safe_form_url,
    scrub_ch_media,
    scrub_job_room,
)

FIXTURES = Path(__file__).parent / "fixtures"
RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"


# --------------------------------------------------------------------------
# Realistischer job-room-Datensatz, so wie ihn die API ausliefert.
# --------------------------------------------------------------------------

RAW_AD: dict[str, Any] = {
    "id": "abc-123",
    "stellennummerEgov": "242499537",
    "owner": {"userId": "u-1", "email": "owner@example.ch"},
    "jobContent": {
        "externalUrl": "https://firma.example/jobs/1",
        "jobDescriptions": [
            {
                "languageIsoCode": "de",
                "title": "Sachbearbeiter/in 80-100%",
                "description": (
                    "Wir suchen Verstärkung.\n"
                    "Kontakt: Barbara Schmucki, Telefon: 044 515 57 61, "
                    "E-Mail: info@med-ipersonal.ch\n"
                    "Bewerbung an bewerbung@firma.ch oder +41 71 955 70 41."
                ),
            }
        ],
        "company": {
            "name": "Muster AG",
            "street": "Bahnhofplatz",
            "postalCode": "8304",
            "city": "Wallisellen",
            "phone": "+41445155761",
            "email": "info@muster.ch",
            "website": "muster.ch",
            "surrogate": True,
        },
        "employment": {"workloadPercentageMin": "80", "workloadPercentageMax": "100"},
        "location": {
            "city": "Zürich",
            "cantonCode": "ZH",
            "coordinates": {"lon": "8.5", "lat": "47.4"},
        },
        "applyChannel": {
            "emailAddress": "barbara.schmucki@muster.ch",
            "phoneNumber": "+41445155761",
            "formUrl": "https://muster.ch/bewerben",
            "rawPostAddress": "Muster AG, Bahnhofplatz 1",
            "additionalInfo": "Sende deine Bewerbung per E-Mail an Phil (phil@muster.ch)",
        },
        "publicContact": {
            "salutation": "MS",
            "firstName": "Barbara",
            "lastName": "Schmucki",
            "phone": "+41445155761",
            "email": "barbara.schmucki@muster.ch",
        },
    },
    "publication": {"startDate": "2026-07-27", "endDate": "2026-08-26"},
}


@pytest.fixture
def scrubbed() -> dict[str, Any]:
    return scrub_job_room(RAW_AD)


class TestJobRoomScrubbing:
    def test_public_contact_is_gone(self, scrubbed: dict[str, Any]) -> None:
        assert "publicContact" not in scrubbed["jobContent"]

    def test_apply_channel_block_is_gone(self, scrubbed: dict[str, Any]) -> None:
        # additionalInfo ist Freitext mit Namen und Mailadressen -> komplett raus
        assert "applyChannel" not in scrubbed["jobContent"]

    def test_owner_is_gone(self, scrubbed: dict[str, Any]) -> None:
        assert "owner" not in scrubbed

    def test_company_contact_fields_are_gone(self, scrubbed: dict[str, Any]) -> None:
        company = scrubbed["jobContent"]["company"]
        assert "email" not in company
        assert "phone" not in company

    def test_no_leaks_reported(self, scrubbed: dict[str, Any]) -> None:
        assert find_contact_leaks(scrubbed) == []

    def test_no_email_or_phone_anywhere(self, scrubbed: dict[str, Any]) -> None:
        blob = json.dumps(scrubbed, ensure_ascii=False)
        assert not EMAIL_RE.search(blob), "E-Mail-Muster überlebt die Bereinigung"
        assert not PHONE_RE.search(blob), "Telefonnummer überlebt die Bereinigung"

    def test_original_is_not_mutated(self) -> None:
        scrub_job_room(RAW_AD)
        assert RAW_AD["jobContent"]["publicContact"]["firstName"] == "Barbara"

    # -- was erhalten bleiben MUSS ----------------------------------------

    def test_keeps_matching_relevant_fields(self, scrubbed: dict[str, Any]) -> None:
        jc = scrubbed["jobContent"]
        assert scrubbed["id"] == "abc-123"
        assert jc["company"]["name"] == "Muster AG"
        assert jc["company"]["surrogate"] is True  # Vermittler-Erkennung
        assert jc["location"]["cantonCode"] == "ZH"
        assert jc["location"]["coordinates"]["lat"] == "47.4"
        assert jc["employment"]["workloadPercentageMin"] == "80"
        assert scrubbed["publication"]["endDate"] == "2026-08-26"

    def test_keeps_application_route_as_flags(self, scrubbed: dict[str, Any]) -> None:
        flags = scrubbed["jobContent"]["applyChannelFlags"]
        assert flags["form_url"] == "https://muster.ch/bewerben"
        assert flags["apply_via_email"] is True
        assert flags["apply_via_phone"] is True
        assert flags["apply_via_post"] is True

    def test_description_survives_but_redacted(self, scrubbed: dict[str, Any]) -> None:
        text = scrubbed["jobContent"]["jobDescriptions"][0]["description"]
        assert "Wir suchen Verstärkung" in text  # Inhalt bleibt fürs Matching
        assert "[E-Mail entfernt]" in text
        assert "[Telefon entfernt]" in text


class TestSafeFormUrl:
    @pytest.mark.parametrize(
        "value",
        [
            # Real gemessen: Arbeitgeber trägt eine Personen-Mail als URL ein
            "http://a.janjic@baumont.ch",
            "https://user:pw@host.ch/x",
            "mailto:info@muster.ch",
            "nicht-mal-eine-url",
            "",
            None,
            123,
        ],
    )
    def test_rejects_bad_values(self, value: object) -> None:
        assert safe_form_url(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "https://muster.ch/bewerben",
            "http://firma.example/jobs/1?ref=x",
        ],
    )
    def test_accepts_plain_urls(self, value: str) -> None:
        assert safe_form_url(value) == value


class TestRedactText:
    def test_removes_emails(self) -> None:
        assert "@" not in redact_text("Schreib an hans.muster@firma.ch bitte")

    @pytest.mark.parametrize(
        "phone",
        ["+41 44 515 57 61", "044 515 57 61", "0041 71 955 70 41", "+41445155761"],
    )
    def test_removes_phone_formats(self, phone: str) -> None:
        assert PHONE_PLACEHOLDER_IN(redact_text(f"Ruf an: {phone}"))

    def test_keeps_ordinary_text(self) -> None:
        text = "Wir suchen eine Person mit 3 Jahren Erfahrung in Python und SQL."
        assert redact_text(text) == text

    def test_passes_through_non_strings(self) -> None:
        assert redact_text(None) is None
        assert redact_text(42) == 42


def PHONE_PLACEHOLDER_IN(text: str) -> bool:
    return "[Telefon entfernt]" in text


class TestChMediaScrubbing:
    def test_contact_blob_removed_but_flagged(self) -> None:
        data = {
            "id": 1087740,
            "title": "Logistiker 100%",
            "contact": 'Luca Biedermann<br /><a href="mailto:l.b@uj.ch">l.b@uj.ch</a><br />+41817580883',
            "activity": "<p>Kontakt: 071 955 70 41</p>",
            "url_application": "https://uj.ch/bewerben",
            "home_office": False,
            "company": {"name": "Universal-Job AG", "description": "Mail: a@b.ch"},
        }
        out = scrub_ch_media(data)
        assert "contact" not in out
        assert out["has_contact_person"] is True
        assert out["title"] == "Logistiker 100%"
        assert out["home_office"] is False
        assert out["url_application"] == "https://uj.ch/bewerben"
        blob = json.dumps(out, ensure_ascii=False)
        assert not EMAIL_RE.search(blob)
        assert not PHONE_RE.search(blob)


# --------------------------------------------------------------------------
# Regressionstest gegen echte, bereits abgelegte Rohdaten
# --------------------------------------------------------------------------


class TestStoredRawData:
    """Prüft, was tatsächlich auf der Platte liegt.

    Übersprungen, solange noch nichts abgerufen wurde — dieser Test ist die
    Absicherung im laufenden Betrieb, nicht nur im Labor.
    """

    def test_no_pii_in_stored_raw_files(self) -> None:
        files = sorted(RAW_DIR.rglob("*.json.gz"))
        if not files:
            pytest.skip("Noch keine Rohdaten abgelegt (jobpipe fetch job-room)")

        offenders: list[str] = []
        for f in files:
            with gzip.open(f, "rb") as fh:
                blob = fh.read().decode("utf-8")
            if EMAIL_RE.search(blob) or PHONE_RE.search(blob):
                offenders.append(str(f.relative_to(RAW_DIR)))
            if find_contact_leaks(json.loads(blob)):
                offenders.append(f"{f.relative_to(RAW_DIR)} (Kontaktfeld)")

        assert not offenders, (
            f"{len(offenders)} von {len(files)} Rohdateien enthalten Kontaktdaten: {offenders[:5]}"
        )

    def test_fixture_is_clean(self) -> None:
        path = FIXTURES / "job_room_search_page.json"
        if not path.exists():
            pytest.skip("Fixture fehlt")
        blob = path.read_text(encoding="utf-8")
        assert not EMAIL_RE.search(blob)
        assert not PHONE_RE.search(blob)
