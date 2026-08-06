"""Kommandozeile.

jobpipe doctor                    robots.txt aller Quellen prüfen
jobpipe fetch job-room            Tagesinkrement
jobpipe fetch job-room --since 7  Backfill
jobpipe status                    letzte Läufe und Bestand
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import numpy as np
import structlog
import typer
from rich.console import Console
from rich.table import Table

from jobpipe.collect.base import PoliteClient, RawCache, RobotsDisallowed, RobotsGate
from jobpipe.collect.ch_media import ChMediaFetcher
from jobpipe.collect.job_room import JobRoomFetcher, JobRoomQuery, ResultWindowExceeded
from jobpipe.config import Config, Profile
from jobpipe.enrich import cluster as cluster_mod
from jobpipe.enrich import embed as embed_mod
from jobpipe.enrich import score as score_mod
from jobpipe.enrich import skills as skills_mod
from jobpipe.output import digest as digest_mod
from jobpipe.output import rate as rate_mod
from jobpipe.output import render as render_mod
from jobpipe.parse import pipeline as parse_pipeline
from jobpipe.store import db, reference
from jobpipe.store import feedback as feedback_store
from jobpipe.store import jobs as jobs_store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Sammelt und rankt Schweizer Stelleninserate.",
)
console = Console()


def _setup_logging() -> None:
    level = os.environ.get("JOBPIPE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
    )


def _client(cfg: Config, dry_run: bool = False) -> PoliteClient:
    p = cfg.politeness
    return PoliteClient(
        user_agent=p.resolved_user_agent(),
        requests_per_second=p.requests_per_second,
        timeout=p.timeout_seconds,
        retry_attempts=p.retry_attempts,
        retry_backoff=p.retry_backoff_seconds,
        robots_cache_minutes=p.robots_cache_minutes,
        dry_run=dry_run,
    )


def _warn_missing_contact() -> None:
    if not os.environ.get("JOBPIPE_CONTACT_URL", "").strip():
        console.print(
            "[yellow]Hinweis:[/] JOBPIPE_CONTACT_URL ist nicht gesetzt. Der User-Agent "
            "enthält dann keine Kontaktadresse.\n"
            "         Setze sie in .env — Portalbetreiber sollen sehen können, wer da "
            "unterwegs ist."
        )


# --------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Prüft robots.txt aller Quellen gegen unseren User-Agent.

    Bricht mit Exit-Code 1 ab, wenn eine Quelle uns inzwischen aussperrt.
    """
    _setup_logging()
    cfg = Config.load()
    _warn_missing_contact()
    ua = cfg.politeness.resolved_user_agent()

    console.print(f"\n[bold]User-Agent:[/] {ua}\n")
    gate = RobotsGate(ua, cfg.politeness.robots_cache_minutes)

    checks: list[tuple[str, str]] = []
    if cfg.sources.job_room.enabled:
        jr = cfg.sources.job_room
        checks.append(("job-room Such-API", f"{jr.base_url}{jr.search_path}"))
        checks.append(("job-room Detail", f"{jr.base_url}/jobadservice/api/jobAdvertisements/x"))
    # Auch inaktive Quellen prüfen: man will wissen, ob eine Quelle offen ist,
    # bevor man sie einschaltet. Der Zustand wird aber ausgewiesen.
    suffix = "" if cfg.sources.ch_media.enabled else " [dim](inaktiv)[/]"
    for name, host in cfg.sources.ch_media.active_hosts().items():
        checks.append((f"{name} Sitemap{suffix}", f"{host.base_url}{host.sitemaps[0]}"))
        checks.append((f"{name} Detailseite{suffix}", f"{host.base_url}/job/beispiel/1"))

    table = Table(show_header=True, header_style="bold")
    table.add_column("Quelle")
    table.add_column("Pfad", overflow="fold")
    table.add_column("robots.txt")
    table.add_column("Crawl-Delay")

    blocked = 0
    for label, url in checks:
        try:
            allowed = gate.allows(url)
            delay = gate.crawl_delay(url)
        except RobotsDisallowed as exc:
            table.add_row(label, url, "[red]nicht prüfbar[/]", str(exc)[:40])
            blocked += 1
            continue
        if not allowed:
            blocked += 1
        table.add_row(
            label,
            url.replace("https://", ""),
            "[green]erlaubt[/]" if allowed else "[red]verboten[/]",
            f"{delay:g}s" if delay else "—",
        )

    console.print(table)
    if blocked:
        console.print(
            f"\n[red]{blocked} Pfad(e) sind für uns gesperrt.[/] "
            "Die betroffene Quelle darf nicht abgefragt werden."
        )
        raise typer.Exit(1)
    console.print("\n[green]Alle geprüften Pfade sind erlaubt.[/]\n")


@app.command()
def fetch(
    source: Annotated[
        str, typer.Argument(help="job-room | myjob | ostjob | zentraljob | ch-media (alle aktiven)")
    ],
    since: Annotated[
        int | None, typer.Option("--since", help="Inserate der letzten N Tage. Default aus Config.")
    ] = None,
    seed: Annotated[
        bool,
        typer.Option(
            "--seed",
            help="Nur Sitemap lesen und IDs vermerken, keine Detailseiten. Für den ersten Lauf.",
        ),
    ] = False,
    backfill: Annotated[
        int,
        typer.Option(
            "--backfill",
            help="Detailseiten aus dem Altbestand pro Lauf (CH Media). 0 = nur Neues.",
        ),
    ] = 0,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Zeigt geplante Requests, sendet nichts.")
    ] = False,
) -> None:
    """Holt Inserate und legt sie roh unter data/raw/ ab."""
    _setup_logging()
    cfg = Config.load()
    _warn_missing_contact()

    key = source.replace("-", "_")
    known_ch = set(cfg.sources.ch_media.hosts)
    if key != "job_room" and key != "ch_media" and key not in known_ch:
        console.print(
            f"[red]Unbekannte Quelle '{source}'.[/] Verfügbar: job-room, ch-media, "
            f"{', '.join(sorted(known_ch))}."
        )
        raise typer.Exit(2)

    if key != "job_room":
        _fetch_ch_media(cfg, key, seed=seed, backfill=backfill, dry_run=dry_run)
        return

    conn = db.init_db(cfg.storage.resolved_db_path())
    cache = RawCache(cfg.storage.resolved_raw_dir())

    with _client(cfg, dry_run=dry_run) as client:
        fetcher = JobRoomFetcher(cfg, client, cache, conn)
        q = JobRoomQuery(
            since_days=since if since is not None else cfg.sources.job_room.default_since_days,
            cantons=list(cfg.region.cantons),
        )

        if dry_run:
            console.print("\n[bold]Trockenlauf[/] — es wird nichts gesendet.\n")
            console.print(f"  Quelle:      {cfg.sources.job_room.base_url}")
            console.print(f"  Kantone:     {', '.join(q.cantons or []) or 'alle'}")
            console.print(f"  Zeitraum:    letzte {q.since_days} Tag(e)")
            console.print(f"  Seitengrösse:{cfg.sources.job_room.page_size}")
            console.print(f"  Drosselung:  {cfg.politeness.requests_per_second} req/s")
            console.print(f"  Body:        {q.to_body()}\n")
            return

        run_id = db.start_run(conn, key, q.to_body())
        try:
            res = fetcher.fetch(query=q)
        except ResultWindowExceeded as exc:
            db.finish_run(conn, run_id, status="failed", error=str(exc))
            console.print(f"\n[red]Abfrage zu gross:[/] {exc}\n")
            raise typer.Exit(1) from exc
        except RobotsDisallowed as exc:
            db.finish_run(conn, run_id, status="failed", error=str(exc))
            console.print(f"\n[red]Durch robots.txt gesperrt:[/] {exc}\n")
            raise typer.Exit(1) from exc
        except Exception as exc:
            db.finish_run(conn, run_id, status="failed", error=str(exc))
            raise

        db.finish_run(
            conn,
            run_id,
            items_seen=res.items_seen,
            requests_made=res.requests_made,
        )
        mb = client.stats.bytes_received / 1_048_576
        console.print(
            f"\n[green]Fertig.[/] {res.items_seen} Inserate in {res.requests_made} Requests "
            f"({mb:.1f} MB) → {cfg.storage.resolved_raw_dir()}\n"
        )


def _fetch_ch_media(cfg: Config, key: str, *, seed: bool, backfill: int, dry_run: bool) -> None:
    """myjob / ostjob / zentraljob — ein Adapter, mehrere Hosts."""
    hosts = cfg.sources.ch_media.hosts
    targets = (
        {k: v for k, v in cfg.sources.ch_media.active_hosts().items()}
        if key == "ch_media"
        else {key: hosts[key]}
    )
    if not targets:
        console.print("[yellow]Keine CH-Media-Quelle aktiv.[/] Siehe config.yaml.")
        return

    conn = db.init_db(cfg.storage.resolved_db_path())
    cache = RawCache(cfg.storage.resolved_raw_dir())

    with _client(cfg, dry_run=dry_run) as client:
        for portal, host in targets.items():
            known = conn.execute(
                "SELECT COUNT(*) FROM source_items WHERE portal = ?", (portal,)
            ).fetchone()[0]
            if known == 0 and not seed and backfill == 0:
                console.print(
                    f"[yellow]{portal}:[/] noch keine IDs bekannt. Der erste Lauf sollte "
                    f"[bold]--seed[/] sein — sonst würden {portal} tausende Detailseiten "
                    "am Stück abgefragt.\n"
                    f"  jobpipe fetch {portal.replace('_', '-')} --seed"
                )
                continue

            run_id = db.start_run(conn, portal, {"seed": seed, "backfill": backfill})
            try:
                fetcher = ChMediaFetcher(portal, host, cfg, client, cache, conn)
                res = fetcher.fetch(seed_only=seed, max_details=backfill)
            except RobotsDisallowed as exc:
                db.finish_run(conn, run_id, status="failed", error=str(exc))
                console.print(f"\n[red]{portal} durch robots.txt gesperrt:[/] {exc}\n")
                raise typer.Exit(1) from exc
            except Exception as exc:
                db.finish_run(conn, run_id, status="failed", error=str(exc))
                raise

            db.finish_run(
                conn,
                run_id,
                items_seen=res.items_seen,
                items_new=res.items_new,
                items_updated=res.items_updated,
                requests_made=res.requests_made,
            )
            pending = conn.execute(
                "SELECT COUNT(*) FROM source_items WHERE portal = ? AND fetched_at IS NULL",
                (portal,),
            ).fetchone()[0]
            console.print(
                f"[green]{portal}:[/] {res.items_seen} in der Sitemap, {res.items_new} neue IDs, "
                f"{res.items_updated} Detailseiten geholt, {pending} offen "
                f"({res.requests_made} Requests)"
            )
            if pending and not seed:
                console.print(
                    f"  [dim]Altbestand aufholen: jobpipe fetch "
                    f"{portal.replace('_', '-')} --backfill 500[/]"
                )


@app.command()
def parse(
    show_duplicates: Annotated[
        bool, typer.Option("--show-duplicates", help="Grösste Vermittler-Gruppen anzeigen.")
    ] = False,
) -> None:
    """Normalisiert die Rohdaten, dedupliziert und schreibt sie in die DB.

    Läuft komplett offline auf data/raw/ — beliebig oft wiederholbar.
    """
    _setup_logging()
    cfg = Config.load()
    conn = db.init_db(cfg.storage.resolved_db_path())
    raw_dir = cfg.storage.resolved_raw_dir()

    if not raw_dir.exists():
        console.print("[yellow]Keine Rohdaten.[/] Erst `jobpipe fetch …` laufen lassen.")
        raise typer.Exit(1)

    report = parse_pipeline.run(conn, raw_dir)

    console.print(
        f"\n[green]Geparst.[/] {report.files_read} Dateien gelesen, "
        f"{report.parsed} Inserate normalisiert"
        + (f", {report.skipped} übersprungen" if report.skipped else "")
    )
    console.print(f"  Geocodiert:              {report.geocoded}/{report.parsed}")
    console.print(
        f"  Stellen nach Dedup:      {report.clusters} "
        f"(davon {report.cross_portal_clusters} auf mehreren Portalen)"
    )
    d = report.company_dedup
    if d:
        console.print(
            f"  Vermittler-Duplikate:    {d['entfernt']} von {d['inserate_vorher']} "
            f"({d['reduktion_prozent']}%), grösste Gruppe: {d['groesste_gruppe']}"
        )
    console.print(
        f"  Gespeichert:             {report.stored_new} neu, {report.stored_updated} aktualisiert\n"
    )

    c = jobs_store.counts(conn)
    table = Table(show_header=True, header_style="bold", title="Bestand")
    table.add_column("Kennzahl")
    table.add_column("Wert", justify="right")
    table.add_row("Inserate gesamt", str(c["total"]))
    table.add_row("davon aktiv", str(c["active"]))
    table.add_row("Personalvermittler", f"{c['agencies']} ({c['agency_share']}%)")
    table.add_row("mit Koordinaten", f"{c['geocoded']} ({c['geocoded_share']}%)")
    table.add_row("eindeutige Stellen", str(c["clusters"]))
    table.add_row(
        "[bold]nach Gruppierung[/]",
        f"[bold]{c['representatives']}[/] (Vermittler-Anteil {c['rep_agency_share']}%)",
    )
    for portal, n in c["per_portal"].items():
        table.add_row(f"  aus {portal}", str(n))
    console.print(table)

    if show_duplicates:
        rows = jobs_store.top_companies(conn, 10)
        t2 = Table(show_header=True, header_style="bold", title="Grösste Inserenten")
        t2.add_column("Firma")
        t2.add_column("Inserate", justify="right")
        t2.add_column("versch. Texte", justify="right")
        for r in rows:
            t2.add_row(r["company_name"][:44], str(r["c"]), str(r["distinct_texts"]))
        console.print(t2)


@app.command()
def score(
    profile_name: Annotated[
        str, typer.Option("--profile", "-p", help="Name der Profildatei in profiles/")
    ],
    gemini: Annotated[
        bool,
        typer.Option(
            "--gemini",
            help="Gemini-Embeddings statt des lokalen Modells. Braucht GOOGLE_API_KEY; "
            "der kostenlose Tier reicht erfahrungsgemäss nicht für den ganzen Bestand, "
            "bei Kontingentende wird automatisch lokal weitergerechnet.",
        ),
    ] = False,
    top: Annotated[int, typer.Option("--top", help="Wie viele Treffer anzeigen.")] = 15,
) -> None:
    """Bettet Inserate ein und rankt sie gegen ein Suchprofil."""
    _setup_logging()
    cfg = Config.load()
    try:
        profile = Profile.load(profile_name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    conn = db.init_db(cfg.storage.resolved_db_path())
    report = score_mod.run(conn, profile, prefer_local=not gemini, use_gemini=gemini, top_n=top)

    if not report.candidates:
        console.print("[yellow]Keine Kandidaten.[/] Erst `jobpipe parse` laufen lassen.")
        raise typer.Exit(1)

    console.print(
        f"\n[bold]{profile.display_name}[/]\n"
        f"  Modell:      {report.model}\n"
        f"  Kandidaten:  {report.candidates}\n"
        f"  Embeddings:  {report.embeddings_cached} aus Cache, "
        f"{report.embeddings_computed} neu berechnet\n"
    )

    table = Table(show_header=True, header_style="bold", title=f"Top {len(report.top)}")
    table.add_column("#", justify="right", width=3)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Stelle", overflow="fold")
    table.add_column("Firma", overflow="fold")
    table.add_column("Ort")
    table.add_column("km", justify="right", width=4)

    for i, hit in enumerate(report.top, 1):
        flags = ""
        if hit["is_agency"]:
            flags += " [dim](Vermittler)[/]"
        if hit["group_size"] > 1:
            flags += f" [dim](+{hit['group_size'] - 1} Orte)[/]"
        table.add_row(
            str(i),
            f"{hit['score']:.3f}",
            hit["title"][:52] + flags,
            hit["company"][:26],
            (hit["city"] or "—")[:16],
            f"{hit['distance_km']:.0f}" if hit["distance_km"] is not None else "—",
        )
    console.print(table)

    console.print("\n[bold]Begründung der ersten drei:[/]")
    for i, hit in enumerate(report.top[:3], 1):
        console.print(f"\n  [bold]{i}. {hit['title'][:64]}[/] — {hit['company'][:34]}")
        console.print(f"     Zielrolle: {hit['role']}  (Cosine {hit['semantic_raw']})")
        for key, reason in hit["reasons"].items():
            if reason:
                console.print(f"     {key:<16} {reason[:96]}")
        console.print(f"     [dim]{hit['url']}[/]")
    console.print()


@app.command()
def digest(
    profile_name: Annotated[
        str, typer.Option("--profile", "-p", help="Name der Profildatei in profiles/")
    ],
    top: Annotated[int, typer.Option("--top", help="Wie viele Treffer in den Digest.")] = 15,
    fmt: Annotated[str, typer.Option("--format", help="md | html | beide")] = "beide",
    out: Annotated[
        Path | None, typer.Option("--out", help="Zielverzeichnis. Default: digests/")
    ] = None,
    preview: Annotated[
        bool,
        typer.Option(
            "--preview",
            help="Nur anzeigen, nichts schreiben und nichts als gesehen markieren.",
        ),
    ] = False,
) -> None:
    """Erzeugt den Digest für ein Suchprofil.

    Ohne `--preview` wird festgehalten, was gezeigt wurde — das ist die
    Grundlage für «neu seit dem letzten Digest».
    """
    _setup_logging()
    cfg = Config.load()
    try:
        profile = Profile.load(profile_name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    conn = db.init_db(cfg.storage.resolved_db_path())
    scored = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE profile = ?", (profile.name,)
    ).fetchone()[0]
    if not scored:
        console.print(
            f"[yellow]Keine Scores für '{profile.name}'.[/] "
            f"Erst `jobpipe score --profile {profile.name}` laufen lassen."
        )
        raise typer.Exit(1)

    model_row = conn.execute("SELECT model FROM embeddings LIMIT 1").fetchone()
    d = digest_mod.build(conn, profile, cfg, top_n=top, record=not preview)
    d.model = model_row["model"] if model_row else ""

    if not d.has_content:
        console.print("[yellow]Keine aktiven Treffer.[/]")
        raise typer.Exit(1)

    cap = cfg.quality.max_per_company_in_digest
    console.print(
        f"\n[bold]{d.profile_title}[/] — {len(d.entries)} Treffer, "
        f"[bold]{d.new_count} neu[/], aus {d.total_candidates} bewerteten Stellen"
    )
    if d.companies_capped:
        console.print(
            f"  [dim]{d.companies_capped} Treffer ausgeblendet (max. {cap} pro Arbeitgeber)[/]"
        )

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("#", justify="right", width=3)
    table.add_column("", width=2)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Stelle", overflow="ellipsis", max_width=46)
    table.add_column("Firma", overflow="ellipsis", max_width=24)
    table.add_column("Ort", overflow="ellipsis", max_width=14)
    table.add_column("km", justify="right", width=4)
    for e in d.entries:
        marks = "🆕" if e.is_new else ""
        table.add_row(
            str(e.rank),
            marks,
            f"{e.score:.3f}",
            e.title + (" [dim](Vermittler)[/]" if e.is_agency else ""),
            e.company,
            e.city or "—",
            f"{e.distance_km:.0f}" if e.distance_km is not None else "—",
        )
    console.print(table)

    if d.expiring:
        console.print(f"\n[bold]Läuft bald ab[/] ({len(d.expiring)}):")
        for e in d.expiring:
            console.print(f"  · {e.title[:56]} — {e.company[:26]}, noch {e.expires_in_days} Tage")

    if preview:
        console.print("\n[dim]Vorschau — nichts geschrieben, nichts als gesehen markiert.[/]\n")
        return

    out_dir = out or (Path(cfg.storage.db_path).parent.parent / "digests")
    written: list[Path] = []
    for f in ("md", "html") if fmt == "beide" else (fmt,):
        if f not in ("md", "html"):
            console.print(f"[red]Unbekanntes Format '{f}'.[/] Erlaubt: md, html, beide.")
            raise typer.Exit(2)
        written.append(render_mod.write(d, out_dir, f, cap))

    conn.execute(
        "UPDATE digest_runs SET path = ? WHERE id = (SELECT MAX(id) FROM digest_runs WHERE profile = ?)",
        (str(written[0]), profile.name),
    )
    console.print("\n[green]Geschrieben:[/]")
    for p in written:
        console.print(f"  {p}")
    console.print()


@app.command()
def daily(
    profiles: Annotated[str, typer.Option("--profiles", help="Kommagetrennt, z.B. lucas,example")],
    top: Annotated[int, typer.Option("--top", help="Treffer pro Digest.")] = 15,
    backfill: Annotated[
        int, typer.Option("--backfill", help="CH-Media-Detailseiten pro Lauf.")
    ] = 0,
) -> None:
    """Der komplette Tageslauf: holen, parsen, ranken, Digest schreiben.

    Für den Cron-Eintrag gedacht. Bricht bei einem Fehler in einer Quelle nicht
    ab — was schon gesammelt ist, wird trotzdem verarbeitet.
    """
    _setup_logging()
    cfg = Config.load()
    _warn_missing_contact()
    names = [n.strip() for n in profiles.split(",") if n.strip()]

    console.print("\n[bold]1/4 Sammeln[/]")
    try:
        fetch("job-room")
    except typer.Exit:
        console.print("  [yellow]job-room übersprungen[/]")
    for portal in cfg.sources.ch_media.active_hosts():
        if not cfg.sources.ch_media.enabled:
            break
        try:
            _fetch_ch_media(cfg, portal, seed=False, backfill=backfill, dry_run=False)
        except typer.Exit:
            console.print(f"  [yellow]{portal} übersprungen[/]")

    console.print("\n[bold]2/4 Normalisieren[/]")
    parse()

    for name in names:
        console.print(f"\n[bold]3/4 Ranken — {name}[/]")
        score(profile_name=name, gemini=False, top=5)
        console.print(f"\n[bold]4/4 Digest — {name}[/]")
        digest(profile_name=name, top=top, fmt="beide", out=None, preview=False)


@app.command()
def rate(
    profile_name: Annotated[
        str, typer.Option("--profile", "-p", help="Name der Profildatei in profiles/")
    ],
    limit: Annotated[int, typer.Option("--limit", help="Wie viele Treffer pro Sitzung.")] = 20,
) -> None:
    """Bewertet die gezeigten Digest-Treffer.

    Eine Taste pro Entscheidung: j interessant, n nein, m vielleicht,
    o im Browser öffnen, s überspringen, q Schluss.

    Aus diesen Bewertungen werden ab M6 die Gewichte kalibriert — bis dahin
    sind sie geraten.
    """
    _setup_logging()
    cfg = Config.load()
    try:
        profile = Profile.load(profile_name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    conn = db.init_db(cfg.storage.resolved_db_path())
    before = feedback_store.stats(conn, profile.name)
    if not before["offen"]:
        console.print(
            f"\n[green]Nichts offen.[/] {before['bewertet']} Treffer bereits bewertet.\n"
            f"Neue kommen mit dem nächsten `jobpipe digest --profile {profile.name}`.\n"
        )
        return

    session = rate_mod.run(conn, profile.name, limit=limit)
    after = feedback_store.stats(conn, profile.name)

    console.print(
        f"\n[green]{session.rated} bewertet[/] "
        f"({session.positive} interessant, {session.maybe} vielleicht, {session.negative} nein)"
        + (f", {session.skipped} übersprungen" if session.skipped else "")
    )
    console.print(f"  Insgesamt bewertet: {after['bewertet']}, noch offen: {after['offen']}")

    need = feedback_store.MIN_RATINGS_FOR_CALIBRATION
    if after["reicht_für_kalibrierung"]:
        sep = feedback_store.separation(conn, profile.name)
        console.print(
            f"  [green]Genug für die Kalibrierung.[/] Score-Abstand "
            f"interessant vs. nein: {sep['abstand']}"
        )
    else:
        console.print(
            f"  [dim]Noch {need - after['bewertet']} Bewertungen bis zur Kalibrierung "
            f"(ab {need} ist sie mehr als Rauschen).[/]"
        )
    if after["ablehnungsgründe"]:
        console.print("\n  [bold]Häufigste Ablehnungsgründe:[/]")
        for reason, n in list(after["ablehnungsgründe"].items())[:4]:
            console.print(f"    {n:>3}× {reason}")
    console.print()


@app.command()
def clusters(
    profile_name: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Gap-Analyse für dieses Profil ergänzen."),
    ] = None,
    min_size: Annotated[int, typer.Option("--min-size", help="Kleinste Clustergrösse.")] = 25,
    top: Annotated[int, typer.Option("--top", help="Wie viele Cluster anzeigen.")] = 12,
    with_agencies: Annotated[
        bool,
        typer.Option(
            "--with-agencies",
            help="Vermittler-Inserate einbeziehen. Standardmässig aus: bei 63 % "
            "Vermittleranteil clustert man sonst deren Textvorlagen statt der Berufe.",
        ),
    ] = False,
) -> None:
    """Gruppiert die Stellen nach Job-Typ und benennt sie.

    Zeigt die Landschaft des Stellenmarkts, nicht nur die Treffer: welche
    Arten von Stellen gibt es, wie gross sind sie, wie heissen sie im
    offiziellen Schweizer Berufsraster.
    """
    _setup_logging()
    cfg = Config.load()
    conn = db.init_db(cfg.storage.resolved_db_path())

    # Vermittler standardmässig raus. Sie stellen 63 % des Bestands und
    # schreiben aus Textbausteinen; ohne Filter entstehen Cluster, die den
    # Schreibstil eines Anbieters abbilden statt einen Berufstyp
    # ("mellingerstrasse, alegro, baden" war ein solcher).
    agency_filter = "" if with_agencies else " AND company_is_agency = 0"
    rows = conn.execute(
        f"""SELECT id, title, company_name, description_md FROM jobs
             WHERE status='active' AND is_group_representative = 1{agency_filter}"""
    ).fetchall()
    if len(rows) < min_size * 2:
        console.print(f"[yellow]Zu wenig Daten[/] ({len(rows)}). Erst mehr sammeln und parsen.")
        raise typer.Exit(1)

    embedder = embed_mod.build_embedder(prefer_local=True)
    cache = embed_mod.EmbeddingCache(conn, embedder)
    texts = [
        embed_mod.build_job_text(r["title"], r["company_name"], r["description_md"] or "")
        for r in rows
    ]
    console.print(f"Bette {len(texts)} Inserate ein (meist aus dem Cache) …")
    vectors = np.vstack(cache.embed(texts))

    report = cluster_mod.analyse(
        conn,
        job_ids=[int(r["id"]) for r in rows],
        vectors=vectors,
        texts=[r["description_md"] or "" for r in rows],
        titles=[r["title"] for r in rows],
        embed_fn=cache.embed,
        min_cluster_size=min_size,
    )

    console.print(
        f"\n[bold]{report.n_clusters} Job-Typen[/] aus {report.total} Stellen "
        f"({report.noise} ohne klare Zuordnung, {report.noise_share}%)\n"
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Stellen", justify="right", width=7)
    table.add_column("Job-Typ", overflow="fold", max_width=42)
    table.add_column("Charakteristisch", overflow="fold", max_width=38)
    for c in report.clusters[:top]:
        table.add_row(str(c.size), c.name, ", ".join(c.terms[:4]))
    console.print(table)

    if not profile_name:
        console.print(
            "\n[dim]Mit --profile <name> zusätzlich die Skill-Lücken im passendsten Cluster.[/]\n"
        )
        return

    try:
        profile = Profile.load(profile_name)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    best = conn.execute(
        """SELECT jc.cluster_label, COUNT(*) n FROM scores s
             JOIN job_clusters jc ON jc.job_rowid = s.job_rowid
            WHERE s.profile = ?
         GROUP BY jc.cluster_label
         ORDER BY AVG(s.final_score) DESC, n DESC LIMIT 1""",
        (profile.name,),
    ).fetchone()
    if not best:
        console.print("[yellow]Keine Scores.[/] Erst `jobpipe score` laufen lassen.")
        return

    skills = skills_mod.load_skills()
    gap = skills_mod.analyse_gap(conn, int(best["cluster_label"]), profile.cv_text, skills)

    console.print(f"\n[bold]Skill-Abgleich — {gap.cluster_name}[/]")
    console.print(f"  {gap.jobs_analysed} Stellen ausgewertet\n")

    if gap.have:
        console.print("  [green]Hast du:[/] " + ", ".join(e.skill for e in gap.have[:12]))
    if gap.gaps:
        t2 = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        t2.add_column("Fehlt dir", max_width=28)
        t2.add_column("gefordert in", justify="right")
        t2.add_column("Kategorie", style="dim")
        for e in gap.gaps[:12]:
            t2.add_row(e.skill, f"{e.share:.0%} der Stellen", e.category)
        console.print("\n")
        console.print(t2)
    else:
        console.print("  [dim]Keine Lücke über der Relevanzschwelle.[/]")

    if gap.career_hints:
        console.print("\n  [bold]Typische Weiterbildungswege:[/]")
        for hint in gap.career_hints:
            console.print(f"    · {hint}")
    console.print()


@app.command()
def dashboard(
    port: Annotated[int, typer.Option("--port", help="Port für den lokalen Server.")] = 8501,
) -> None:
    """Startet das Dashboard im Browser.

    Läuft lokal auf der SQLite-Datei, schickt nichts ins Netz. Bewerten
    passiert hier beim Durchsehen — deutlich schneller als eine eigene
    Bewertungssitzung.
    """
    import subprocess
    import threading
    from pathlib import Path as _Path

    app_file = _Path(__file__).parent / "output" / "dashboard.py"
    url = f"http://127.0.0.1:{port}"
    console.print(f"\n[bold]Dashboard startet[/] → {url}\n")
    console.print("[dim]Beenden mit Ctrl-C.[/]\n")
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(app_file),
            "--server.port",
            str(port),
            # NUR localhost. Streamlit bindet sonst an alle Schnittstellen und
            # macht das Dashboard im ganzen Netz erreichbar — samt CV-Texten
            # und Profildaten. Im Log stand prompt eine öffentliche URL.
            "--server.address",
            "127.0.0.1",
            # Headless, weil Streamlit sonst beim ersten Start interaktiv nach
            # einer E-Mail-Adresse fragt ("Welcome to Streamlit!"). Ohne
            # Terminal am anderen Ende blockiert der Prozess auf dieser Abfrage
            # und beendet sich, ohne je einen Port zu öffnen. Den Browser
            # öffnen wir stattdessen selbst.
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        check=False,
    )


def _open_when_ready(url: str, timeout_s: float = 30.0) -> None:
    """Öffnet den Browser, sobald der Server antwortet.

    Sofortiges Öffnen zeigt eine Fehlerseite: Streamlit braucht ein paar
    Sekunden, bis es lauscht.
    """
    import socket
    import time
    import webbrowser
    from urllib.parse import urlparse

    parsed = urlparse(url)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((parsed.hostname or "127.0.0.1", parsed.port or 80)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.4)


@app.command("scrub-raw")
def scrub_raw(
    check_only: Annotated[
        bool, typer.Option("--check", help="Nur prüfen, nichts schreiben.")
    ] = False,
) -> None:
    """Wendet die aktuellen PII-Regeln auf bereits gespeicherte Rohdaten an.

    Wenn die Regeln nachgeschärft werden, sind die alten Dateien noch nach den
    alten Regeln bereinigt. Dieser Befehl zieht sie nach — ohne die Portale
    erneut anzufassen.
    """
    import gzip

    from jobpipe.parse.pii import (
        EMAIL_RE,
        PHONE_RE,
        find_contact_leaks,
        scrub_ch_media,
        scrub_job_room,
    )

    _setup_logging()
    cfg = Config.load()
    raw_dir = cfg.storage.resolved_raw_dir()
    if not raw_dir.exists():
        console.print("[yellow]Keine Rohdaten.[/]")
        return

    checked = 0
    dirty = 0
    for portal_dir in sorted(p for p in raw_dir.iterdir() if p.is_dir()):
        portal = portal_dir.name
        scrub = scrub_job_room if portal == "job_room" else scrub_ch_media
        for path in portal_dir.rglob("*.json.gz"):
            checked += 1
            with gzip.open(path, "rb") as fh:
                blob = fh.read().decode("utf-8")
            data = json.loads(blob)
            has_pii = bool(
                EMAIL_RE.search(blob) or PHONE_RE.search(blob) or find_contact_leaks(data)
            )
            if not has_pii:
                continue
            dirty += 1
            if check_only:
                continue
            cleaned = scrub(data)
            with gzip.open(path, "wb") as fh:
                fh.write(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode())

    if check_only:
        style = "red" if dirty else "green"
        console.print(f"[{style}]{dirty}[/] von {checked} Rohdateien enthalten Kontaktdaten.")
        if dirty:
            raise typer.Exit(1)
    else:
        console.print(f"[green]Bereinigt.[/] {dirty} von {checked} Rohdateien nachgezogen.")


@app.command("import-reference")
def import_reference(
    berufe_path: Annotated[
        Path | None,
        typer.Option(
            "--berufe", help="Pfad zum berufsberatung-Datensatz. Default: DSP-Vorprojekt."
        ),
    ] = None,
) -> None:
    """Lädt Referenzdaten: Berufe-Taxonomie und PLZ-Koordinaten.

    Die PLZ-Tabelle wird aus den bereits abgerufenen job-room-Rohdaten
    abgeleitet — führe also erst `jobpipe fetch job-room` aus.
    """
    _setup_logging()
    cfg = Config.load()
    conn = db.init_db(cfg.storage.resolved_db_path())

    try:
        n_berufe = reference.import_berufe(conn, berufe_path)
        console.print(f"[green]Berufe:[/] {n_berufe} importiert")
    except FileNotFoundError as exc:
        console.print(f"[yellow]Berufe übersprungen:[/] {exc}")
        n_berufe = 0

    n_plz = reference.build_plz_table(conn, cfg.storage.resolved_raw_dir())
    if n_plz:
        console.print(f"[green]PLZ-Koordinaten:[/] {n_plz} Ortschaften aus den Rohdaten abgeleitet")
    else:
        console.print(
            "[yellow]PLZ-Tabelle leer.[/] Erst `jobpipe fetch job-room` laufen lassen — "
            "die Koordinaten kommen aus den Inseraten selbst."
        )

    if n_berufe:
        top = conn.execute(
            """SELECT berufsfeld, COUNT(*) c FROM berufe
                WHERE berufsfeld IS NOT NULL
             GROUP BY berufsfeld ORDER BY c DESC LIMIT 5"""
        ).fetchall()
        console.print("\n[bold]Grösste Berufsfelder:[/]")
        for r in top:
            console.print(f"   {r['berufsfeld']:<40} {r['c']}")


@app.command()
def status() -> None:
    """Zeigt die letzten Läufe und den Rohdatenbestand."""
    _setup_logging()
    cfg = Config.load()
    db_path = cfg.storage.resolved_db_path()
    if not db_path.exists():
        console.print(
            "[yellow]Noch keine Datenbank.[/] Erst `jobpipe fetch job-room` laufen lassen."
        )
        return

    conn = db.init_db(db_path)
    rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 10").fetchall()

    table = Table(title="Letzte Läufe", show_header=True, header_style="bold")
    for col in ("Start", "Quelle", "Status", "Gesehen", "Requests"):
        table.add_column(col)
    for r in rows:
        colour = {"ok": "green", "failed": "red"}.get(r["status"], "yellow")
        table.add_row(
            r["started_at"][:16].replace("T", " "),
            r["source"],
            f"[{colour}]{r['status']}[/]",
            str(r["items_seen"]),
            str(r["requests_made"]),
        )
    console.print(table)

    raw = cfg.storage.resolved_raw_dir()
    if raw.exists():
        files = list(raw.rglob("*.json.gz"))
        size = sum(f.stat().st_size for f in files) / 1_048_576
        days = sorted({p.parent.name for p in files})
        console.print(
            f"\nRohdaten: [bold]{len(files)}[/] Dateien, {size:.1f} MB, "
            f"{len(days)} Tag(e)" + (f" ({days[0]} … {days[-1]})" if days else "")
        )
    n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    console.print(f"Normalisiert in der DB: [bold]{n_jobs}[/] (Parser folgt in M2)\n")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
