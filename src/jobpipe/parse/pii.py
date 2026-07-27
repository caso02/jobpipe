"""Entfernt Personendaten, bevor irgendetwas auf Platte landet.

Absichtlich beim **Abruf** angewendet, nicht erst beim Parsen: was wir nie
verwenden wollen, soll auch nie gespeichert werden. Datenminimierung heisst
nicht, unerwünschte Felder später zu ignorieren, sondern sie gar nicht erst
zu behalten.

Was die Portale von sich aus mitliefern:

* job-room  ``jobContent.publicContact``  — Anrede, Vor-, Nachname, Telefon, E-Mail
* job-room  ``jobContent.applyChannel``   — E-Mail und Telefon der Bewerbungsstelle
* job-room  ``jobContent.company``        — E-Mail und Telefon der Firma
* CH Media  ``vacancyDetails.data.contact`` — Name + Mail + Telefon als HTML-Blob

Bewerbungs-URLs (``formUrl``, ``externalUrl``, ``url_application``) bleiben
erhalten — dort bewirbt man sich ohnehin über ein Formular, ohne dass ein
Personenname in der Datenbank liegen muss. Ist eine Stelle nur per Mail oder
Telefon zu erreichen, merken wir uns lediglich *dass* das so ist
(``apply_via_email`` / ``apply_via_phone``); die Adresse selbst liest man im
Originalinserat nach.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

#: E-Mail-Adressen. Auch in Freitext zuverlässig erkennbar.
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")

#: Schweizer und internationale Telefonnummern in gängigen Schreibweisen.
PHONE_RE = re.compile(r"(?:\+41|0041|\b0)\s?\(?\d{1,2}\)?[\s./-]?\d{3}[\s./-]?\d{2}[\s./-]?\d{2}\b")

EMAIL_PLACEHOLDER = "[E-Mail entfernt]"
PHONE_PLACEHOLDER = "[Telefon entfernt]"

#: Schlüssel, die auf jeder Ebene entfernt werden.
#:
#: Die Liste ist absichtlich breit. Gemessen an echten CH-Media-Daten tauchte
#: ``mainPhoneNumber`` in 300 von 383 Inseraten auf — ein Feld, das in der
#: ersten Fassung schlicht fehlte. Deshalb zusätzlich die Endungs-Heuristik in
#: :func:`_is_contact_key` und die rekursive Textredaktion.
CONTACT_KEYS = frozenset(
    {
        "email",
        "emailAddress",
        "phone",
        "phoneNumber",
        "mainPhoneNumber",
        "telephone",
        "tel",
        "mobile",
        "mobilePhone",
        "fax",
        "faxNumber",
        "firstName",
        "lastName",
        "salutation",
        "rawPostAddress",
        "postAddress",
        "contact",
        "contactPerson",
    }
)

#: Schlüsselendungen, die auf Kontaktdaten hindeuten. Fängt Feldnamen ab, die
#: beim Schreiben dieser Liste noch nicht bekannt waren.
_CONTACT_KEY_SUFFIXES = ("phonenumber", "emailaddress", "faxnumber", "mobilenumber")

#: Schlüssel, deren Werte NICHT redigiert werden: URLs und Bildpfade können
#: Ziffernfolgen enthalten, die dem Telefonmuster ähneln.
_URLISH_KEYS = ("url", "link", "href", "website", "image", "logo", "src", "path", "id")

#: Ganze Teilbäume, die restlos verschwinden.
#: ``applyChannel`` fliegt komplett raus — sein ``additionalInfo`` ist Freitext,
#: in dem gemessen Namen und Mailadressen stehen ("… per E-Mail an Phil …").
#: Was davon gebraucht wird, überlebt in ``applyChannelFlags``.
CONTACT_BLOCKS = frozenset({"publicContact", "owner", "jobCenterUserId", "applyChannel", "contact"})


def safe_form_url(value: Any) -> str | None:
    """Gibt die URL nur zurück, wenn sie keine Kontaktdaten transportiert.

    Manche Arbeitgeber tragen im ``formUrl``-Feld eine Mailadresse ein
    (gemessen: ``http://a.janjic@baumont.ch``). Das ist eine Personen-Mail in
    URL-Kleidung — wird verworfen, ebenso URLs mit ``userinfo@host``.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    url = value.strip()
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if "@" in parts.netloc or EMAIL_RE.search(url):
        return None
    return url


def redact_text(text: Any) -> Any:
    """Ersetzt Mailadressen und Telefonnummern in Freitext.

    Wird auf Inseratstexte angewandt. Die Platzhalter tragen kein semantisches
    Signal über die Stelle, das Matching verliert dadurch nichts.

    Grenze, die ehrlich benannt gehört: **Personennamen im Fliesstext bleiben
    stehen.** Sie sind nicht zuverlässig von Firmen-, Produkt- oder Ortsnamen
    zu unterscheiden, und ein aggressiver Filter würde den Text zerstören, den
    wir fürs Matching brauchen. Entfernt werden die maschinell eindeutig
    erkennbaren Kontaktkanäle.
    """
    if not isinstance(text, str):
        return text
    out = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return PHONE_RE.sub(PHONE_PLACEHOLDER, out)


def scrub_job_room(ad: dict[str, Any]) -> dict[str, Any]:
    """Bereinigt ein job-room-Inserat.

    Gibt eine Kopie zurück; das Original bleibt unangetastet.
    """
    out = deepcopy(ad)
    content = out.get("jobContent")
    if not isinstance(content, dict):
        return _strip_dict(out)

    # Bevor die Kanäle entfernt werden: festhalten, DASS es sie gibt.
    # Die Adressen selbst liest man im Originalinserat nach.
    apply_channel = content.get("applyChannel")
    if isinstance(apply_channel, dict):
        content["applyChannelFlags"] = {
            "apply_via_email": bool(apply_channel.get("emailAddress")),
            "apply_via_phone": bool(apply_channel.get("phoneNumber")),
            "apply_via_post": bool(
                apply_channel.get("rawPostAddress") or apply_channel.get("postAddress")
            ),
            "form_url": safe_form_url(apply_channel.get("formUrl")),
        }

    # externalUrl kann dieselbe Mail-in-URL-Falle enthalten wie formUrl.
    if "externalUrl" in content:
        content["externalUrl"] = safe_form_url(content.get("externalUrl"))

    # Inseratstexte: Mail- und Telefonmuster ersetzen, Rest unangetastet.
    descriptions = content.get("jobDescriptions")
    if isinstance(descriptions, list):
        for desc in descriptions:
            if isinstance(desc, dict):
                for key in ("title", "description"):
                    if key in desc:
                        desc[key] = redact_text(desc[key])

    return _strip_dict(out)


def scrub_ch_media(data: dict[str, Any]) -> dict[str, Any]:
    """Bereinigt einen CH-Media-Datensatz (myjob / ostjob / zentraljob).

    Dort steckt der Kontakt als HTML-Blob in ``contact`` — inklusive Name,
    ``mailto:``-Link und Telefonnummer.
    """
    out = deepcopy(data)
    out["has_contact_person"] = bool(out.get("contact"))
    for key in ("url_application", "url_description"):
        if key in out:
            out[key] = safe_form_url(out.get(key))
    for key in ("activity", "requirements", "title"):
        if key in out:
            out[key] = redact_text(out[key])
    company = out.get("company")
    if isinstance(company, dict) and "description" in company:
        company["description"] = redact_text(company["description"])
    return _strip_dict(out)


def _strip_dict(node: dict[str, Any]) -> dict[str, Any]:
    """Typisierte Hülle um :func:`_strip_recursive` für die Scrub-Funktionen."""
    result: dict[str, Any] = _strip_recursive(node)
    return result


def _is_contact_key(key: str) -> bool:
    if key in CONTACT_KEYS or key in CONTACT_BLOCKS:
        return True
    lowered = key.lower()
    return any(lowered.endswith(suffix) for suffix in _CONTACT_KEY_SUFFIXES)


def _is_real_url(value: str) -> bool:
    """Echte http(s)-URL — nur die bleibt vollständig unangetastet.

    Der Feldname allein taugt nicht als Kriterium: gemessen trug
    ``vacanciesExtraLink`` den Wert ``"mailto: personal@kliniken-valens.ch"``.
    Ein Feld, das "Link" heisst, kann eine Mailadresse enthalten.
    """
    return value.startswith(("http://", "https://", "//"))


def _skip_phone_redaction(key: str, value: str) -> bool:
    """Bei ID- und Pfadfeldern keine Telefonmuster suchen.

    Hex-IDs und Pfade können Ziffernfolgen enthalten, die dem Muster ähneln.
    Für E-Mails gilt diese Ausnahme nicht — die sind eindeutig.
    """
    lowered = key.lower()
    return any(hint in lowered for hint in _URLISH_KEYS) or _is_real_url(value)


def _strip_recursive(node: Any, key: str = "") -> Any:
    """Entfernt Kontaktfelder und redigiert Mail-/Telefonmuster in allen Texten.

    Die Redaktion läuft rekursiv über **jeden** String, nicht nur über bekannte
    Beschreibungsfelder: Portale betten Kontaktdaten auch in HTML-Blobs und in
    Felder ein, die man vorher nicht kennt. URL-artige Werte bleiben
    unangetastet, damit Bewerbungslinks intakt bleiben.
    """
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for k, value in node.items():
            if _is_contact_key(k):
                continue
            cleaned[k] = _strip_recursive(value, k)
        return cleaned
    if isinstance(node, list):
        return [_strip_recursive(v, key) for v in node]
    if isinstance(node, str):
        if _is_real_url(node):
            return node
        out = EMAIL_RE.sub(EMAIL_PLACEHOLDER, node)
        if not _skip_phone_redaction(key, node):
            out = PHONE_RE.sub(PHONE_PLACEHOLDER, out)
        return out
    return node


def find_contact_leaks(node: Any, path: str = "") -> list[str]:
    """Sucht übrig gebliebene Kontaktfelder. Grundlage des PII-Tests.

    Meldet Pfade, deren Schlüssel nach Kontaktdaten aussieht und die einen
    nicht-leeren Wert tragen.
    """
    leaks: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if (key in CONTACT_KEYS or key in CONTACT_BLOCKS) and value:
                leaks.append(here)
            leaks.extend(find_contact_leaks(value, here))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            leaks.extend(find_contact_leaks(value, f"{path}[{i}]"))
    return leaks
