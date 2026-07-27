"""Streamlit-Dashboard.

Start über ``jobpipe dashboard``. Läuft lokal, greift direkt auf die SQLite-
Datei zu, schickt nichts ins Netz.

Der wichtigste Unterschied zum Digest: hier wird **beim Anschauen bewertet**.
Eine separate Bewertungssitzung macht niemand zweimal; ein Daumen neben dem
Treffer, den man ohnehin gerade liest, schon.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import streamlit as st

from jobpipe.config import Config, Profile
from jobpipe.parse.geo import haversine_km
from jobpipe.parse.schema import strip_html, unescape_markdown
from jobpipe.store import feedback as fb

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


# --------------------------------------------------------------------------
# Daten
# --------------------------------------------------------------------------


@st.cache_resource
def get_conn(db_path: str) -> sqlite3.Connection:
    """Eine Verbindung für die ganze Sitzung.

    ``check_same_thread=False``, weil Streamlit Reruns in wechselnden Threads
    ausführt.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def load_hits(conn: sqlite3.Connection, profile: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT j.id, j.title, j.company_name, j.city, j.canton, j.lat, j.lon,
                  j.company_is_agency, j.agency_reason, j.description_md,
                  j.description_truncated, j.workload_min, j.workload_max,
                  j.home_office, j.posted_at, j.expires_at, j.source_url,
                  j.apply_url, j.portal, j.group_size,
                  s.final_score, s.semantic_raw, s.best_role, s.rules_json,
                  s.distance_km,
                  jc.cluster_label, jc.cluster_name,
                  f.rating, f.reason
             FROM scores s
             JOIN jobs j ON j.id = s.job_rowid
        LEFT JOIN job_clusters jc ON jc.job_rowid = j.id
        LEFT JOIN feedback f ON f.job_rowid = j.id AND f.profile = s.profile
            WHERE s.profile = ? AND j.status = 'active'
              AND j.is_group_representative = 1
         ORDER BY s.final_score DESC""",
        (profile,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        rules = json.loads(r["rules_json"] or "{}")
        out.append(
            {
                # sqlite3.Row hat kein dict-Interface; keys() ist hier die API,
                # nicht der SIM118-Fall.
                **{k: r[k] for k in r.keys()},  # noqa: SIM118
                "reasons": {k: v for k, v in (rules.get("reasons") or {}).items() if v},
                "scores": rules.get("scores") or {},
            }
        )
    return out


def snippet(text: str, limit: int = 400) -> str:
    clean = unescape_markdown(strip_html(text or ""))
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut) + " …"


# --------------------------------------------------------------------------
# Reiter
# --------------------------------------------------------------------------


def render_hit(conn: sqlite3.Connection, profile: Profile, hit: dict[str, Any]) -> None:
    """Eine Trefferkarte mit Bewertungsknöpfen."""
    job_id = hit["id"]
    rated = hit["rating"]

    title = hit["title"]
    badge = ""
    if hit["company_is_agency"]:
        badge += " `Vermittler`"
    if hit["group_size"] and hit["group_size"] > 1:
        badge += f" `{hit['group_size']} Orte`"
    if rated is not None:
        badge += f" {'👍' if rated == 1 else '🤔' if rated == 0 else '👎'}"

    with st.container(border=True):
        head, score_col = st.columns([6, 1])
        with head:
            st.markdown(f"**[{title}]({hit['source_url']})**{badge}")
            place = hit["city"] or "Ort unbekannt"
            if hit["distance_km"] is not None:
                place += f" · {hit['distance_km']:.0f} km"
            lo, hi = hit["workload_min"], hit["workload_max"]
            pensum = "Pensum offen" if lo is None and hi is None else f"{lo or 0}-{hi or 100}%"
            st.caption(f"{hit['company_name']} · {place} · {pensum}")
        with score_col:
            st.metric("Score", f"{hit['final_score']:.3f}", label_visibility="collapsed")

        st.write(snippet(hit["description_md"]))

        if hit["reasons"]:
            with st.expander("Warum dieser Score?"):
                for key, why in hit["reasons"].items():
                    st.markdown(f"**{CRITERION_LABELS.get(key, key)}** — {why}")
                if hit["cluster_name"]:
                    st.caption(f"Job-Typ: {hit['cluster_name']}")
                if hit["best_role"]:
                    st.caption(f"Passt zu Zielrolle: {hit['best_role']}")
                if hit["agency_reason"]:
                    st.caption(f"Vermittler erkannt über: {hit['agency_reason']}")

        cols = st.columns([1, 1, 1, 3, 2])
        if cols[0].button("👍", key=f"up{job_id}", help="interessant"):
            fb.save(conn, fb.Rating(profile.name, job_id, 1, score_seen=hit["final_score"]))
            st.rerun()
        if cols[1].button("🤔", key=f"mid{job_id}", help="vielleicht"):
            fb.save(conn, fb.Rating(profile.name, job_id, 0, score_seen=hit["final_score"]))
            st.rerun()
        if cols[2].button("👎", key=f"down{job_id}", help="nein"):
            st.session_state[f"reason_open_{job_id}"] = True

        if st.session_state.get(f"reason_open_{job_id}"):
            with cols[3]:
                keys = list(fb.REJECT_REASONS)
                label = st.selectbox(
                    "Warum nicht?",
                    keys,
                    format_func=lambda k: fb.REJECT_REASONS[k],
                    key=f"reason{job_id}",
                )
                if st.button("Speichern", key=f"save{job_id}"):
                    fb.save(
                        conn,
                        fb.Rating(
                            profile.name, job_id, -1, reason=label, score_seen=hit["final_score"]
                        ),
                    )
                    st.session_state[f"reason_open_{job_id}"] = False
                    st.rerun()

        if hit["apply_url"] and hit["apply_url"] != hit["source_url"]:
            cols[4].link_button("Bewerben", hit["apply_url"])


def tab_hits(conn: sqlite3.Connection, profile: Profile, hits: list[dict[str, Any]]) -> None:
    st.subheader("Treffer")
    if not hits:
        st.info("Keine Treffer. Erst `jobpipe score --profile …` laufen lassen.")
        return

    unrated = [h for h in hits if h["rating"] is None]
    st.caption(
        f"{len(hits)} bewertete Stellen · {len(unrated)} noch nicht von dir beurteilt · "
        f"ab {fb.MIN_RATINGS_FOR_CALIBRATION} Bewertungen wird kalibriert"
    )
    for hit in hits[: st.session_state.get("page_size", 25)]:
        render_hit(conn, profile, hit)


def tab_clusters(conn: sqlite3.Connection, hits: list[dict[str, Any]]) -> None:
    st.subheader("Job-Typen")
    rows = conn.execute("SELECT * FROM clusters ORDER BY size DESC").fetchall()
    if not rows:
        st.info("Noch keine Cluster. `jobpipe clusters` laufen lassen.")
        return

    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "Job-Typ": r["name"],
                "Stellen": r["size"],
                "Berufsfeld": r["berufsfeld"] or "—",
                "Begriffe": ", ".join(json.loads(r["terms"] or "[]")[:5]),
            }
            for r in rows
        ]
    )
    # Ohne explizite Breiten quetscht Streamlit alle Spalten auf die erste
    # zusammen und schneidet die Namen ab.
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Job-Typ": st.column_config.TextColumn(width="large"),
            "Stellen": st.column_config.NumberColumn(width="small"),
            "Berufsfeld": st.column_config.TextColumn(width="medium"),
            "Begriffe": st.column_config.TextColumn(width="large"),
        },
    )

    # Wie verteilen sich meine Treffer über die Job-Typen?
    counts: dict[str, int] = {}
    for h in hits[:200]:
        name = h["cluster_name"] or "ohne Zuordnung"
        counts[name] = counts.get(name, 0) + 1
    if counts:
        st.markdown("**Wo liegen meine 200 besten Treffer?**")
        st.bar_chart(pd.Series(counts).sort_values(ascending=False).head(12))


def tab_skills(conn: sqlite3.Connection, profile: Profile, hits: list[dict[str, Any]]) -> None:
    st.subheader("Skill-Lücken")
    from jobpipe.enrich import skills as sk

    labels = [h["cluster_label"] for h in hits[:200] if h["cluster_label"] is not None]
    if not labels:
        st.info("Noch keine Cluster-Zuordnung. `jobpipe clusters` laufen lassen.")
        return

    from collections import Counter

    top_label = Counter(labels).most_common(1)[0][0]
    skills = sk.load_skills()
    if not skills:
        st.warning("Kein Skill-Wörterbuch gefunden (data/ref/skills.yaml).")
        return

    gap = sk.analyse_gap(conn, int(top_label), profile.cv_text, skills)
    st.caption(f"{gap.cluster_name} · {gap.jobs_analysed} Stellen ausgewertet")

    left, right = st.columns(2)
    with left:
        st.markdown("**Hast du**")
        for e in gap.have[:15]:
            st.write(f"{e.skill} — in {e.share:.0%} der Stellen")
        if not gap.have:
            st.caption("nichts erkannt")
    with right:
        st.markdown("**Fehlt dir**")
        for e in gap.gaps[:15]:
            st.write(f"{e.skill} — gefordert in {e.share:.0%}")
        if not gap.gaps:
            st.caption("keine Lücke über der Relevanzschwelle")

    if gap.career_hints:
        st.markdown("**Typische Weiterbildungswege**")
        for hint in gap.career_hints:
            st.write(f"· {hint}")


def tab_calibration(conn: sqlite3.Connection, profile: Profile) -> None:
    st.subheader("Kalibrierung")
    stats = fb.stats(conn, profile.name)
    sep = fb.separation(conn, profile.name)

    c1, c2, c3 = st.columns(3)
    c1.metric("Bewertet", stats["bewertet"])
    c2.metric("Offen", stats["offen"])
    c3.metric(
        "Score-Abstand",
        f"{sep['abstand']:.3f}" if sep["abstand"] is not None else "—",
        help="Mittlerer Score der interessanten Treffer minus der abgelehnten. "
        "Ist er nicht deutlich positiv, hilft kein Feintuning der Gewichte.",
    )

    need = fb.MIN_RATINGS_FOR_CALIBRATION
    if stats["bewertet"] < need:
        st.progress(stats["bewertet"] / need)
        st.info(
            f"Noch {need - stats['bewertet']} Bewertungen. Darunter bestimmen einzelne "
            "Ausreisser das Ergebnis — eine Kalibrierung wäre Zufall."
        )
    elif sep["abstand"] is not None and sep["abstand"] <= 0:
        st.error(
            "Der Score trennt noch nicht zwischen interessant und nein. Bevor an den "
            "Gewichten gedreht wird, gehören die Zielrollen im Profil überarbeitet."
        )
    else:
        st.success("Genug Daten. Gewichtsvorschläge unten.")
        _weight_suggestions(conn, profile)

    if stats["ablehnungsgründe"]:
        st.markdown("**Ablehnungsgründe**")
        import pandas as pd

        st.bar_chart(pd.Series(stats["ablehnungsgründe"]))
        st.caption(
            "Jeder Grund zeigt auf ein Gewicht: «zu weit weg» auf den Ortsanteil, "
            "«falscher Beruf» auf den semantischen Anteil."
        )


def _weight_suggestions(conn: sqlite3.Connection, profile: Profile) -> None:
    """Welche Gewichte die Ablehnungen infrage stellen.

    Bewusst ein Vorschlag, keine Automatik: die Gewichte gehören dem Profil,
    und eine Änderung soll man sehen und verstehen, bevor sie wirkt.
    """
    from collections import Counter

    rows = fb.rated_scores(conn, profile.name)
    reasons = Counter(r for rating, _s, r in rows if rating == -1 and r)
    if not reasons:
        st.caption("Keine Ablehnungsgründe erfasst.")
        return

    total = sum(reasons.values())
    lines = []
    for reason, n in reasons.most_common():
        weight = fb.REASON_TO_WEIGHT.get(reason)
        if not weight:
            continue
        share = n / total
        current = getattr(profile.weights, weight, None)
        if current is None:
            continue
        # Je häufiger ein Grund, desto stärker sollte das zugehörige Gewicht
        # wirken. Der Faktor ist bewusst zahm.
        suggested = round(current * (1 + share), 2)
        lines.append(
            {
                "Grund": fb.REJECT_REASONS.get(reason, reason),
                "Anteil": f"{share:.0%}",
                "Gewicht": weight,
                "aktuell": current,
                "Vorschlag": suggested,
            }
        )
    if lines:
        import pandas as pd

        st.dataframe(pd.DataFrame(lines), width="stretch", hide_index=True)
        st.caption(
            f"Übernehmen: Werte in profiles/{profile.name}.yaml eintragen und "
            f"`jobpipe score --profile {profile.name}` neu laufen lassen."
        )


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="jobpipe", page_icon="🔎", layout="wide")
    cfg = Config.load()
    conn = get_conn(str(cfg.storage.resolved_db_path()))

    available = Profile.list_available()
    if not available:
        st.error("Kein Profil in profiles/. Siehe profiles/README.md.")
        return

    # Profile mit Daten zuerst. Sonst startet das Dashboard mit dem
    # alphabetisch ersten — meist "example" — und zeigt eine leere Seite.
    with_scores = {
        r["profile"] for r in conn.execute("SELECT DISTINCT profile FROM scores").fetchall()
    }
    ordered = sorted(available, key=lambda n: (n not in with_scores, n))

    with st.sidebar:
        st.title("jobpipe")
        name = st.selectbox(
            "Profil",
            ordered,
            format_func=lambda n: n if n in with_scores else f"{n} (keine Scores)",
        )
        profile = Profile.load(name)
        st.caption(profile.display_name)

        st.divider()
        st.markdown("**Filter**")
        min_score = st.slider("Mindest-Score", 0.0, 1.0, 0.0, 0.05)
        max_km = st.slider("Radius (km)", 0, 80, 60, 5)
        workload = st.slider("Pensum", 0, 100, (profile.workload_min, profile.workload_max), 10)
        show_agencies = st.checkbox("Personalvermittler zeigen", value=True)
        only_unrated = st.checkbox("Nur noch nicht Bewertete", value=False)

        cluster_names = [
            r["name"]
            for r in conn.execute("SELECT name FROM clusters ORDER BY size DESC").fetchall()
        ]
        chosen_cluster = st.selectbox("Job-Typ", ["alle", *cluster_names])

        st.session_state["page_size"] = st.slider("Treffer anzeigen", 10, 100, 25, 5)

    hits = load_hits(conn, profile.name)

    def keep(h: dict[str, Any]) -> bool:
        if h["final_score"] < min_score:
            return False
        if not show_agencies and h["company_is_agency"]:
            return False
        if only_unrated and h["rating"] is not None:
            return False
        if chosen_cluster != "alle" and h["cluster_name"] != chosen_cluster:
            return False
        lo = h["workload_min"] if h["workload_min"] is not None else 0
        hi = h["workload_max"] if h["workload_max"] is not None else 100
        if hi < workload[0] or lo > workload[1]:
            return False
        if h["lat"] is not None and profile.locations:
            best = min(
                (
                    haversine_km(h["lat"], h["lon"], loc.lat, loc.lon)
                    for loc in profile.locations
                    if loc.lat and loc.lon
                ),
                default=None,
            )
            if best is not None and best > max_km:
                return False
        return True

    filtered = [h for h in hits if keep(h)]

    t1, t2, t3, t4 = st.tabs(["Treffer", "Job-Typen", "Skill-Lücken", "Kalibrierung"])
    with t1:
        tab_hits(conn, profile, filtered)
    with t2:
        tab_clusters(conn, filtered)
    with t3:
        tab_skills(conn, profile, filtered)
    with t4:
        tab_calibration(conn, profile)


if __name__ == "__main__":
    main()
