"""Digest nach Markdown oder HTML rendern."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader

from jobpipe.output.digest import Digest
from jobpipe.parse.schema import unescape_markdown

TEMPLATE_DIR = Path(__file__).parent / "templates"

Format = Literal["md", "html"]

#: Interne Regelnamen auf lesbare Beschriftungen.
CRITERION_LABELS = {
    "workload": "Pensum",
    "location": "Ort",
    "flextime": "Arbeitszeit",
    "home_office": "Homeoffice",
    "keywords": "Stichworte",
    "recency": "Aktualität",
    "exclude_penalty": "Ausschluss",
    "agency_penalty": "Vermittler",
    "seniority_penalty": "Anforderungsniveau",
}

_WS_RE = re.compile(r"\s+")


def _kriterium(key: str) -> str:
    return CRITERION_LABELS.get(key, key)


def _kurz(text: str, limit: int = 90) -> str:
    """Kürzt und glättet.

    Zitate aus Inseraten enthalten Zeilenumbrüche, Markdown-Escapes und
    gelegentlich ein Pipe — letzteres würde die Markdown-Tabelle zerlegen.
    """
    clean = unescape_markdown(str(text))
    clean = _WS_RE.sub(" ", clean).replace("|", "/").strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _datum(value: datetime) -> str:
    wochentage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    return f"{wochentage[value.weekday()]}, {value.strftime('%d.%m.%Y')}"


def _autoescape(template_name: str | None) -> bool:
    """Autoescape für HTML-Templates einschalten.

    Jinjas ``select_autoescape`` entscheidet über die Dateiendung — unsere
    Templates heissen aber ``digest.html.j2``, enden also auf ``.j2``. Damit
    war Autoescape stillschweigend **aus**.

    Das ist keine Kosmetik: Stellentitel und Firmennamen stammen von fremden
    Portalen. Ein Inserat mit ``<script>`` im Titel würde beim Öffnen des
    Digests im Browser ausgeführt. Markdown bleibt bewusst unescaped, dort
    wären HTML-Entities nur Störung.
    """
    if not template_name:
        return False
    return template_name.endswith((".html.j2", ".html", ".htm", ".xml"))


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=_autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["kriterium"] = _kriterium
    env.filters["kurz"] = _kurz
    env.filters["datum"] = _datum
    return env


def render(digest: Digest, fmt: Format = "md", cap: int = 3) -> str:
    env = build_environment()
    template = env.get_template(f"digest.{fmt}.j2")
    return template.render(digest=digest, cap=cap)


def write(digest: Digest, out_dir: Path, fmt: Format = "md", cap: int = 3) -> Path:
    """Schreibt den Digest. Dateiname enthält Profil und Datum."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = digest.created_at.strftime("%Y-%m-%d")
    path = out_dir / f"{stamp}_{digest.profile_name}.{fmt}"
    path.write_text(render(digest, fmt, cap), encoding="utf-8")
    return path
