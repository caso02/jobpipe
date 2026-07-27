"""Interaktives Bewerten der Digest-Treffer.

Muss schnell gehen. Wer pro Inserat mehr als ein paar Sekunden braucht, hört
nach dem dritten Tag auf — und dann fehlen die Daten, mit denen die Gewichte
in M6 kalibriert werden sollen. Deshalb: eine Taste pro Entscheidung, der
Ablehnungsgrund als Ziffer, alles andere optional.
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from jobpipe.parse.schema import strip_html, unescape_markdown
from jobpipe.store import feedback as fb

console = Console()

KEYS_HELP = "[bold]j[/] interessant · [bold]n[/] nein · [bold]m[/] vielleicht · [bold]o[/] öffnen · [bold]s[/] überspringen · [bold]q[/] Schluss"


@dataclass
class Session:
    rated: int = 0
    skipped: int = 0
    positive: int = 0
    negative: int = 0
    maybe: int = 0
    aborted: bool = False


def _snippet(text: str, limit: int = 320) -> str:
    clean = unescape_markdown(strip_html(text))
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut) + " …"


def _show(row: sqlite3.Row, index: int, total: int) -> None:
    header = Text()
    header.append(f"{index}/{total}  ", style="dim")
    header.append(row["title"], style="bold")

    body = Text()
    body.append(row["company_name"], style="bold")
    if row["company_is_agency"]:
        body.append("  (Personalvermittler)", style="dim")
    body.append("\n")
    place = row["city"] or "Ort unbekannt"
    if row["distance_km"] is not None:
        place += f"  ·  {row['distance_km']:.0f} km"
    lo, hi = row["workload_min"], row["workload_max"]
    pensum = "Pensum offen" if lo is None and hi is None else f"{lo or 0}-{hi or 100}%"
    body.append(f"{place}  ·  {pensum}  ·  Score {row['final_score']:.3f}\n", style="dim")
    if row["best_role"]:
        body.append(f"passt zu: {row['best_role']}\n", style="dim italic")
    body.append("\n")
    body.append(_snippet(row["description_md"] or ""))

    rules = json.loads(row["rules_json"] or "{}")
    reasons = {k: v for k, v in (rules.get("reasons") or {}).items() if v}
    if reasons:
        body.append("\n\n")
        for key, why in list(reasons.items())[:4]:
            body.append(f"  {key}: ", style="dim")
            body.append(f"{why[:88]}\n", style="dim")

    console.print(Panel(body, title=header, title_align="left", border_style="dim"))


def _ask_reason() -> str | None:
    """Strukturierter Ablehnungsgrund. Eine Ziffer genügt."""
    console.print("  [dim]Warum nicht?[/]")
    keys = list(fb.REJECT_REASONS)
    for i, key in enumerate(keys, 1):
        console.print(f"    [bold]{i}[/] {fb.REJECT_REASONS[key]}")
    choice = Prompt.ask("  Ziffer", default="", show_default=False).strip()
    if not choice.isdigit():
        return None
    idx = int(choice) - 1
    return keys[idx] if 0 <= idx < len(keys) else None


def run(
    conn: sqlite3.Connection,
    profile_name: str,
    limit: int = 20,
    ask_reason: bool = True,
) -> Session:
    """Führt durch die offenen Treffer."""
    rows = fb.pending(conn, profile_name, limit)
    session = Session()
    if not rows:
        return session

    console.print(f"\n[bold]{len(rows)} offene Treffer[/] — {KEYS_HELP}\n")

    for i, row in enumerate(rows, 1):
        _show(row, i, len(rows))
        answer = Prompt.ask(
            "  ", choices=["j", "n", "m", "o", "s", "q"], default="s", show_choices=False
        ).lower()

        while answer == "o":
            webbrowser.open(row["source_url"])
            answer = Prompt.ask(
                "  ", choices=["j", "n", "m", "o", "s", "q"], default="s", show_choices=False
            ).lower()

        if answer == "q":
            session.aborted = True
            break
        if answer == "s":
            session.skipped += 1
            continue

        rating = {"j": 1, "m": 0, "n": -1}[answer]
        reason = _ask_reason() if (rating == -1 and ask_reason) else None

        fb.save(
            conn,
            fb.Rating(
                profile=profile_name,
                job_rowid=int(row["id"]),
                rating=rating,
                reason=reason,
                score_seen=float(row["final_score"]),
            ),
        )
        session.rated += 1
        if rating == 1:
            session.positive += 1
        elif rating == 0:
            session.maybe += 1
        else:
            session.negative += 1

    return session
