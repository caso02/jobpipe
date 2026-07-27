# jobpipe

Sammelt Schweizer Stelleninserate, normalisiert sie in ein gemeinsames Schema
und rankt sie gegen ein persönliches Suchprofil — mit Embedding-basiertem
Matching, regelbasierten Kriterien und Clustering der Job-Typen.

Gebaut für die eigene Stellensuche in den Kantonen **ZH, SG und TG**.

> **Status:** M0–M6 stehen — Collection, Parsing, Deduplizierung, Embeddings,
> Scoring, täglicher Digest, Clustering, Skill-Gap-Analyse und Dashboard.
> Offen ist nur noch die Kalibrierung der Gewichte; sie braucht rund 40
> Bewertungen und die sammelt man im Dashboard nebenbei.
> Der vollständige Recherche- und Architekturplan steht in [PLAN.md](PLAN.md).

---

## Was dieses Projekt bewusst *nicht* tut

Diese Liste steht absichtlich zuoberst. Bei einem Werkzeug, das fremde Websites
ausliest, ist der Umgang damit die eigentliche Designentscheidung — nicht die
Anzahl der Datenquellen.

**Kein jobs.ch.** Technisch wäre es einfach: die Detailseiten sind
server-seitig gerendert, enthalten `schema.org/JobPosting`-Daten und stehen zu
43'792 Stück in der eigenen Sitemap. Die Nutzungsbedingungen der JobCloud AG
untersagen automatisierten Zugriff jedoch wörtlich (Ziff. 7):

> «Crawler, Scraper oder Data-Mining-Tools einsetzen oder Daten von der Website
> extrahieren, reproduzieren, kopieren, verkaufen, verwerten oder
> wiederverkaufen»

Damit ist die Sache entschieden, unabhängig davon, was robots.txt erlaubt.
Dasselbe gilt für jobup.ch und jobscout24.ch (gleicher Betreiber) sowie für
jobagent.ch, dessen robots.txt pauschal `Disallow: /` setzt.

**Keine Umgehung von Schutzmassnahmen.** Kein Lösen von CAPTCHAs, kein
Rotieren von Proxys, kein Vortäuschen eines Browsers. Der User-Agent nennt das
Projekt und eine Kontakt-URL. Wo eine Quelle uns aussperrt, ist das eine
Antwort und kein Hindernis — `respect_robots: false` ist nicht implementiert
und wirft beim Laden der Konfiguration einen Fehler.

**Keine Personendaten.** Die Portale liefern Namen, E-Mail-Adressen und
Telefonnummern von Recruitern gleich mit — `publicContact` bei job-room,
`contact` bei den CH-Media-Portalen. Diese Felder werden beim Parsen verworfen,
und zwar über eine **Allowlist**: das Zielschema listet auf, was übernommen
wird, alles andere kommt gar nicht erst an. Behalten wird die Bewerbungs-URL,
denn dort bewirbt man sich ohnehin über ein Formular.

Kommerzielle Scraper für dieselben Portale extrahieren `contact_firstname`,
`contact_lastname` und `contact_email` und sind unter «Lead generation»
kategorisiert. Genau das ist hier nicht das Ziel.

**Keine Weiterverbreitung.** Inseratsinhalte bleiben lokal. `data/` ist
gitignored, die Datenbank wird nicht veröffentlicht. Das Repo enthält Code,
keine fremden Daten.

**Höflichkeit ist Konfiguration, nicht Vorsatz.** Ein Token-Bucket begrenzt auf
1 Request/Sekunde pro Host, robots.txt wird bei *jedem* Lauf neu geprüft,
Rohdaten werden gecacht, damit Entwicklung und Tests die Portale nicht erneut
belasten. Das Tagesinkrement bei job-room sind zwei Requests.

---

## Datenquellen

| Quelle | Rolle | Zugang |
|---|---|---|
| **job-room.ch** | primär — beste Struktur | Öffentliche JSON-API (SECO / arbeit.swiss), Geokoordinaten, Pensum-Range, Ablaufdatum |
| **myjob.ch** | primär — grösste Abdeckung | Sitemap + `window.__PRELOADED_STATE__` |
| **ostjob.ch** | sekundär (SG/TG) | identischer Adapter |
| **zentraljob.ch** | optional | identischer Adapter |

Die drei CH-Media-Portale teilen sich Technik, robots.txt und Betreiberin —
ein Adapter bedient alle.

Zur Herkunft der job-room-Daten: die Inserate werden dort **von den Firmen
selbst eingeliefert** (69 % über die offizielle Publikations-API, 21 % aus
angebundenen Fremdsystemen, 8 % über das Webformular). Es ist kein Aggregator.
Nur 1.5 % der Inserate unterliegen der Stellenmeldepflicht — der Rest wird
freiwillig publiziert, weil die Plattform für Arbeitgeber kostenlos ist.

---

## Setup

```bash
python3.13 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # JOBPIPE_CONTACT_URL eintragen
cp profiles/example.yaml profiles/meinname.yaml
```

`uv` funktioniert ebenso (`uv sync`), ist aber nicht erforderlich.

---

## Nutzung

```bash
jobpipe doctor                        # robots.txt aller Quellen gegen unseren UA prüfen
jobpipe import-reference              # Berufe-Taxonomie und PLZ-Koordinaten laden

jobpipe fetch job-room --since 7      # einmaliger Backfill
jobpipe fetch job-room                # Tagesinkrement (~663 Inserate, 2 Requests)
jobpipe fetch job-room --dry-run      # zeigt geplante Requests, sendet nichts

jobpipe fetch ostjob --seed           # erster Lauf: nur IDs merken, 1 Request
jobpipe fetch ostjob                  # danach: nur neue Inserate
jobpipe fetch ostjob --backfill 500   # Altbestand in Etappen nachholen

jobpipe parse --show-duplicates       # normalisieren, deduplizieren, speichern
jobpipe score --profile meinname      # einbetten und gegen das Profil ranken
jobpipe digest --profile meinname     # Digest als Markdown und HTML
jobpipe rate --profile meinname       # Treffer bewerten (Grundlage der Kalibrierung)
jobpipe clusters --profile meinname   # Job-Typen und Skill-Lücken
jobpipe dashboard                     # alles interaktiv im Browser
jobpipe scrub-raw --check             # prüfen, dass keine Kontaktdaten gespeichert sind
jobpipe status                        # Läufe und Bestand
```

### Dashboard

```bash
jobpipe dashboard
```

Vier Reiter: **Treffer** (gefilterte Rangliste mit Score-Begründung und
Bewertungsknöpfen), **Job-Typen**, **Skill-Lücken**, **Kalibrierung**.

Bewertet wird beim Durchsehen — eine separate Bewertungssitzung macht niemand
zweimal, ein Daumen neben dem Treffer, den man ohnehin gerade liest, schon.
Ab 40 Bewertungen schlägt der Kalibrierungs-Reiter neue Gewichte vor,
hergeleitet aus den Ablehnungsgründen: «zu weit weg» zieht am Ortsgewicht,
«falscher Beruf» am semantischen Anteil.

Der Server bindet ausschliesslich an `127.0.0.1`. Streamlit würde sonst an
allen Netzwerkschnittstellen lauschen und das Dashboard samt CV-Texten im
ganzen Netz erreichbar machen.

### Clustering: warum ohne Vermittler

`jobpipe clusters` schliesst Personalvermittler standardmässig aus. Grund: sie
stellen 63 % des Bestands und schreiben aus Textbausteinen. Mit ihnen entstehen
Cluster, die den **Schreibstil eines Anbieters** abbilden statt einen Berufstyp
— gemessen etwa eine Gruppe mit den Merkmalen „mellingerstrasse, alegro, baden".
`--with-agencies` schaltet sie zu.

Cluster heissen `Berufsfeld — charakteristische Begriffe`, etwa
„Verkehr, Logistik, Sicherheit — logistiker, lager, umschlag". Die **konkrete**
Berufsbezeichnung aus der Taxonomie wird ermittelt, aber bewusst nicht im Namen
verwendet: sie ist unzuverlässig. Derselbe Logistik-Cluster bekam als nächsten
Beruf „Bedienungs- und Schalterpersonal (Seilbahnen/Skilifte)" — richtiges
Berufsfeld, falsche Rolle darin. Das Berufsfeld ist grob genug, um zu stimmen;
die Begriffe sind spezifisch genug, um etwas zu sagen. Die Zwischenebene trägt
nicht.

### Täglich

```bash
jobpipe daily --profiles lucas,example --backfill 300
```

Holt, parst, rankt und schreibt die Digests nach `digests/`. Als Cron-Eintrag:

```bash
0 7 * * * cd ~/projekte/bewerbung && ./.venv/bin/jobpipe daily --profiles lucas,example --backfill 300 >> data/cron.log 2>&1
```

Der Digest zeigt zu jedem Treffer, **warum** er dort steht — Pensum, Distanz,
Gleitzeit mit zitierter Fundstelle, Stichworte. Ein Ranking, dessen
Zustandekommen man nicht sieht, benutzt man nach einer Woche nicht mehr.

Höchstens drei Stellen pro Arbeitgeber (`quality.max_per_company_in_digest`).
Ohne diese Deckelung bestand die Top-12 gemessen aus fünf Inseraten desselben
Vermittlers — korrekt gerankt und trotzdem unbrauchbar.

«Neu» heisst: seit dem letzten Digest **noch nicht gezeigt** — nicht «neu im
Bestand». Ein Inserat kann seit Tagen dort liegen und erst heute durch
veränderte Konkurrenz nach oben rutschen.

### Personalvermittler: warum eine eigene Erkennung

Rund **63 % des Bestands** stammt von Personalvermittlern. Wer das nicht
behandelt, bekommt eine Trefferliste, die zur Hälfte aus Vermittler-Rauschen
besteht.

job-room hat ein Feld dafür — `company.surrogate` —, aber es ist eine
**Selbstdeklaration und damit unbrauchbar**: Yellowshark, Randstad, job impuls,
Excellent Personaldienstleistungen und Alegro Personal setzen alle
`surrogate = False`. Der darauf gestützte Malus griff bei genau den Firmen
nicht, bei denen er nötig gewesen wäre.

Ersatz ist ein kombiniertes Signal (`jobpipe.parse.agency`) aus Firmenname,
Ausnahme für öffentliche Arbeitgeber und einem Verhaltensmuster in den Daten.
Der Kern des Musters ist nicht die Berufsvielfalt allein — ein Kanton schreibt
ebenfalls über zwanzig Berufsarten aus —, sondern deren Kombination mit der
**Ortsstreuung**:

| Firma | Berufscodes | Orte | Streuung |
|---|---|---|---|
| Kanton Zürich | 21 | 1 | 0.0 km |
| Stadt Zürich | 15 | 1 | 0.5 km |
| Yellowshark | 20 | 17 | 23.1 km |
| Jobup | 75 | 51 | 22.4 km |

Ein Kanton stellt in seinen eigenen Amtsstellen an, ein Vermittler platziert
bei Kunden in der ganzen Region. Jede Einstufung speichert ihre Begründung in
`jobs.agency_reason` — nachvollziehbar statt magisch.

### Embeddings: lokal als Standard

`jobpipe score` nutzt standardmässig **sentence-transformers lokal** — offline,
kostenlos, kein Kontingent. Rund 3'600 Inserate brauchen etwa 50 Sekunden auf
einem M-Mac; ein zweiter Lauf ist dank Cache praktisch instantan.

`--gemini` schaltet auf `gemini-embedding-001` um. Gemessen reicht der
kostenlose Tier dafür nicht: schon der zweite Batch antwortete mit
`429 RESOURCE_EXHAUSTED`. Die Pipeline bricht deswegen nicht ab, sondern
rechnet ab diesem Punkt lokal weiter.

Zwei Eigenheiten, die im Code dokumentiert sind, weil sie leicht zu übersehen
sind:

- **Gekürzte Gemini-Vektoren sind nicht normalisiert.** Bei 768 statt 3072
  Dimensionen liegt die Norm bei ~0.59. Ohne erneute L2-Normalisierung ist das
  Skalarprodukt keine Cosine-Ähnlichkeit mehr.
- **Deutsche Bürotexte liegen semantisch eng beieinander.** Gemessen ergibt
  "Sachbearbeiterin Innendienst" gegen "Empfangsmitarbeiterin Frontdesk" eine
  Cosine von 0.86 — obwohl genau das die Unterscheidung ist, auf die es
  ankommt. Die Rohwerte werden deshalb innerhalb des Kandidatenpools
  rangnormalisiert, sonst wäre der semantische Anteil praktisch konstant.

### Warum `--seed` beim ersten CH-Media-Lauf

myjob.ch führt 53'031 Inserate in seiner Sitemap, ostjob.ch 6'119. Alle
Detailseiten am Stück zu holen wäre bei 1 Request/Sekunde ein 15-Stunden-Crawl
— unverhältnismässig gegenüber dem Betreiber.

`lastmod` taugt nicht als Änderungssignal (bei myjob tragen 43'952 Einträge
dasselbe Datum aus einer Massen-Regenerierung). Die IDs sind dagegen aufsteigend
vergeben. `--seed` merkt sich deshalb in einem einzigen Request alle bekannten
IDs, ohne eine Detailseite zu laden; danach holt jeder Lauf nur noch, was
wirklich neu ist. `--backfill N` arbeitet den Altbestand in Etappen ab.

---

## Architektur

```
1 Collection    Fetcher je Quelle, gedrosselt, Rohdaten-Cache
2 Parsing       gemeinsames Schema, PII-Allowlist, Dedup, Geocoding
3 Enrichment    Embeddings + Regelscore + Clustering   (pro Profil)
4 Output        täglicher Digest, später Dashboard     (pro Profil)
```

Schicht 1 und 2 sind profil-unabhängig, Schicht 3 und 4 laufen pro Profil.
Ein zweites Profil kostet dadurch fast keine zusätzlichen Embedding-Kosten.

Details, Messwerte und die Begründung der Quellenauswahl: [PLAN.md](PLAN.md).

---

## Lizenz

MIT — siehe [LICENSE](LICENSE). Gilt für den Code. Die abgerufenen
Inseratsdaten gehören den jeweiligen Rechteinhabern und werden nicht
mitverteilt.
