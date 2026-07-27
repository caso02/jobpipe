# PLAN.md — Schweizer Stellenmarkt-Pipeline

> Recherche- und Architekturplan. Stand: 27.07.2026. Alle Messwerte und
> HTTP-Befunde in diesem Dokument wurden an diesem Datum gegen die Live-
> Portale verifiziert und sind im Abschnitt «Verifikation» reproduzierbar.


## Context

Ziel ist eine Pipeline, die Schweizer Stelleninserate sammelt, normalisiert und
gegen den eigenen CV rankt — für die Bewerbungsrunde in ~1 Jahr (Wirtschafts-
informatik / Data Science) und gleichzeitig als öffentliches Portfolio-Stück auf
GitHub.

Vor der Umsetzung musste geklärt werden, **welche Quellen überhaupt zulässig und
technisch tragfähig sind**. Die Recherche hat das Bild gegenüber der
Ausgangsannahme deutlich verschoben:

- **jobs.ch scheidet aus** — nicht technisch, sondern **vertraglich**. Die
  Nutzungsbedingungen verbieten Scraping wörtlich und unmissverständlich.
- **job-room.ch ist deutlich besser als erwartet.** Die Annahme, die Job-Room-API
  diene nur dem Publizieren, ist **für `api.job-room.ch` korrekt** — aber es gibt
  eine **zweite, öffentliche und unauthentifizierte Such-API**
  (`/jobadservice/api/…`), die die SPA selbst nutzt und die reichhaltigste
  strukturierte Daten aller drei Portale liefert (inkl. Geokoordinaten,
  Pensum-Range, Publikations-Enddatum).

Entschieden (in diesem Gespräch bestätigt): jobs.ch **ganz weglassen**,
Zielkantone **ZH, SG, TG**, Output **erst Digest, dann Dashboard**. Die Pipeline
bedient **zwei Suchprofile** — Wirtschaftsinformatik/Data Science (Zürich/
Winterthur) und KV/Administration (Frauenfeld) — auf demselben Datenbestand.

Zusätzlich geprüft: das Vorprojekt unter `~/zhaw/DSP`. Der dortige Scraper ist
für dieses Projekt **nicht** wiederverwendbar (anderes Ziel, andere Struktur) —
der dort erarbeitete **Berufe-Datensatz** dagegen sehr wohl. Details in
Abschnitt 3.

---

## 1. Machbarkeit pro Portal

### 🔴 jobs.ch — **nicht empfohlen** (raus aus dem Projekt)

Technisch wäre es einfach. Rechtlich ist es eindeutig verboten.

**Nutzungsbedingungen für Stellensuchende, Ziff. 7** (JobCloud AG, Stand März
2026, [Quelle](https://www.jobs.ch/de/nutzungsbedingungen/)) — Nutzer:innen
dürfen niemals direkt oder indirekt:

> «auf die Website anders als über die von JobCloud öffentlich bereitgestellte
> Web- oder App-Schnittstelle […] zugreifen;»
>
> «Crawler, Scraper oder Data-Mining-Tools einsetzen oder Daten von der Website
> extrahieren, reproduzieren, kopieren, verkaufen, verwerten oder wiederverkaufen;»
>
> «Automatisierung, Scripting, Bots oder anderer Tools zur Automatisierung von
> Abläufen oder zum Zugriff auf Dienste verwenden (ausser über offiziell
> bereitgestellte Tools von JobCloud);»
>
> «automatisiert oder ohne Befugnis auf Daten, insbesondere auf Personendaten,
> zugreifen;»

Das ist kein Graubereich. Ein öffentliches Portfolio-Repo mit einem
jobs.ch-Scraper wäre ein dokumentierter Verstoss — mit deinem Namen dran, in
genau der Branche, in der du dich bewirbst.

**Bemerkenswert:** robots.txt und AGB widersprechen sich hier.

| Befund | Ergebnis |
|---|---|
| robots.txt | `Disallow: /api/`, `/api_proxy/`. Detailseiten sind **erlaubt**: die Regel `/de/stellenangebote/detail/*/*/*` verlangt ≥3 Pfadsegmente, echte URLs haben nur eines (`/detail/<uuid>/`) |
| Sitemap | `https://www.jobs.ch/sitemaps/jobs/de/job/sitemap.job-de.xml.gz` — **43'792 Inserat-URLs**, also technisch eine Einladung zum Crawlen |
| Rendering | Voll server-seitig (`x-ssr-universal-build`-Header). Kein `__NEXT_DATA__`. Zwei `application/ld+json`-Blöcke mit `schema.org/JobPosting` |
| Felder (JSON-LD) | `title`, `description` (HTML), `datePosted`, `hiringOrganization.name`, `jobLocation.address`, `industry`, `employmentType`, `occupationalCategory` |
| Felder (DOM) | stabile `data-cy`-Hooks: `vacancy-title`, `info-publication`, `info-workload` (z.B. `100%`), `info-contract`, `info-salary_estimate`, `apply-button-external` |
| Lohn | **Nur eine Schätzung von jobs.ch selbst** (`info-salary_estimate`), nicht vom Arbeitgeber. Kein `baseSalary` im JSON-LD |
| Anti-Bot | CloudFront + **AWS WAF Captcha** (`captcha-sdk.awswaf.com` im Seiten-Config). Einzelrequests gehen durch; bei höherer Rate greift die Challenge |
| URL-Stabilität | Sauber: `404` bei unbekannter UUID, `200` bei aktiv. Detailseiten bleiben lange erreichbar |
| Volumen | 416 Treffer für «Wirtschaftsinformatik» |

**Fazit:** technisch grün, vertraglich rot. Rot gewinnt. → wird nicht gebaut.

---

### 🟡 ostjob.ch / myjob.ch / zentraljob.ch (CH Media) — **heikel, aber vertretbar**

> Die folgende Analyse wurde an **ostjob.ch** durchgeführt und gilt 1:1 auch für
> **myjob.ch** und **zentraljob.ch**: verifiziert identische robots.txt,
> identische Sitemap-Struktur, identisches `__PRELOADED_STATE__`-Objekt, selbe
> Betreiberin (CH Media Classifieds AG), selbe AGB. Volumen: myjob **>50'000**,
> ostjob 6'119, zentraljob 1'720.

Kein explizites Scraping-Verbot in den Nutzungsbedingungen für Stellensuchende —
aber deutliche Signale, dass Aggregatoren unerwünscht sind.

| Befund | Ergebnis |
|---|---|
| robots.txt | `User-agent: *` → `Allow: /`, danach `Disallow: /api`, `/export`, `/vacancy/topjobs`, `/minisite`, `/job/preview`. **`/job/<slug>/<id>` ist erlaubt** |
| robots.txt (Signal) | Namentlich gesperrt: `x28-job-bot`, `JobRoboter`, `Jobsearch`, `burroboot`, `toweya`, `seoscanners` — allesamt Job-Aggregatoren. Die interne API (`/api`) ist ebenfalls gesperrt |
| Sitemap | `https://www.ostjob.ch/sitemap-vacancies.xml` — **6'119 Inserate**, mit `lastmod`. Vollständige Enumeration ohne Suchseiten-Crawling möglich |
| Nutzungsbedingungen | **Keine** Crawler-/Bot-Klausel. §6 Geistiges Eigentum: nicht-exklusives, nicht übertragbares Nutzungsrecht; «Eine weitergehende Veröffentlichung, Nutzung, Weitergabe oder Vervielfältigung […] ohne ausdrückliche Zustimmung […] ist nicht gestattet.» → zielt auf **Weiterverbreitung**, nicht auf privates Lesen |
| AGB (Inserenten) | Ziff. 13.1: «Die nicht autorisierte und ohne gewichtige Eigenleistung erfolgende Bearbeitung und Verwertung von […] Inseraten durch Dritte ist unzulässig» → richtet sich gegen Aggregatoren, die Inserate 1:1 weiterverwerten |
| Rendering | React, server-seitig gerendert. **`window.__PRELOADED_STATE__`** enthält alles als sauberes JSON — deutlich besserer Parse-Target als DOM oder JSON-LD |
| Anti-Bot | Nur nginx. Kein Cloudflare, kein CF-Ray, keine WAF, kein Login-Wall |
| URL-Stabilität | **Sehr sauber:** `410 Gone` bei abgelaufenen Inseraten, `200` bei aktiven. Der Slug ist dekorativ — nur die numerische ID zählt (`/job/BELIEBIGER-SLUG/1087740` → 200) |

**Felder aus `__PRELOADED_STATE__.vacancyDetails.data`:**

```
id, title, company_id, workplace_zip, workplace_city,
activity (HTML), requirements (HTML),
contact  ← Recruiter-Name + Mail + Telefon  ⚠️ VERWERFEN (revDSG)
url_application, url_description,
type_key, type_value_min, type_value_max,   ← Pensum-Range
date_actualization, date_start, date_first_published,
home_office (bool),  ← direkt für dein Homeoffice-Kriterium
is_promoted, is_special_offer, external_id,
company { id, name, description, … }, categories[], address_country
```

**Zwei relevante Einschränkungen:**

1. **Kein Lohnfeld.**
2. **iframe-Inserate.** Ein Teil der Ads bettet die Beschreibung per iframe von
   der Arbeitgeber-Domain ein (z.B. `universaljob.ch/de/job-frame/VA-425-WW9`).
   Für diese Inserate ist auf ostjob nur eine Kurzfassung vorhanden. → Der
   iframe wird **nicht** verfolgt (fremde Domain, eigene robots.txt/AGB);
   stattdessen Flag `description_truncated: true` setzen.

**Fazit:** Gelb. Bauen — aber konservativ: Sitemap-getrieben statt Suchseiten-
Crawling, ehrlicher identifizierbarer User-Agent, ≤1 req/s, aggressives Caching
über `lastmod`, keine Weiterverbreitung der Inhalte.

---

### 🟢 job-room.ch — **gut** → **primäre Datenquelle**

#### Zur Ausgangsfrage: bestätigt *und* widerlegt

**Bestätigt:** Die offizielle, dokumentierte
[Job-Room Jobs API](https://test-api.job-room.ch/api-docs/jobAdvertisements/v1/)
(`https://api.job-room.ch/jobAdvertisements/v1`) ist **eine Publikations-API für
Arbeitgeber**, nicht zum Auslesen des Stellenpools:

- Zweck ist die Erfüllung der **Stellenmeldepflicht** (seit 1.7.2018).
- Die Lese-Operationen sind **auf die eigenen Inserate beschränkt** —
  «Get all job advertisements belonging to the owner», `POST /_search` filtert
  nur nach `status` der eigenen Ads.
- Zugangsdaten muss man bei SECO beantragen (`jobroom-api@seco.admin.ch`) unter
  Angabe von Firmenname, Adresse, technischem Kontakt und **«expected volume of
  job registrations»** — also explizit als publizierendes Unternehmen.
- Verifiziert: alles unter `https://api.job-room.ch/api/` antwortet mit
  `401 Unauthorized`, ebenso `/v3/api-docs` und `/swagger-ui.html`.

**Widerlegt:** Es gibt eine **zweite, davon getrennte, öffentliche Such-API** —
die, die die job-room-Weboberfläche selbst benutzt:

```
POST https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search
     ?page=0&size=100&sort=date_desc
Content-Type: application/json
```

Verifiziert mit reinem `curl`, **ohne Auth, ohne Browser, ohne Cookies, ohne
Sonder-Header** → `200 OK`. Ebenso öffentlich:

```
GET /jobadservice/api/jobAdvertisements/{uuid}
GET /jobadservice/api/jobAdvertisements/byStellennummerEgov/{nr}
```

| Befund | Ergebnis |
|---|---|
| robots.txt (`www` und `api`) | `# Do not crawl Job Adverts` / `Disallow: /job-search/`, `/aav/confirmation`. Gesperrt ist die **SPA-Route**; `/jobadservice/` ist **nicht** gelistet |
| Rendering | Angular-SPA. Daten kommen aus obiger JSON-API — kein HTML-Parsing nötig |
| Anti-Bot | Keines erkennbar. Kein Cloudflare, keine WAF, kein Rate-Limit-Header |
| Paginierung | `x-total-count`-Header + `link`-Header (`next`/`last`/`first`). `size` bis **500** getestet OK |
| Harte Grenze | **`(page+1) × size ≤ 10'000`** (Elasticsearch `max_result_window`). Darüber `HTTP 412` mit der Meldung *«Please make use of provided filters to narrow your search result.»* |
| URL-Stabilität | `404` bei unbekannter UUID. Zusätzlich **explizites `publication.endDate`** im Datensatz — Ablauf ist bekannt, statt geraten |
| Lohn | **Kein Lohnfeld** (wie überall) |

**Filter im Request-Body** (alle verifiziert):

```json
{
  "keywords": ["Wirtschaftsinformatik"],
  "cantonCodes": ["ZH", "SG", "TG", "AR", "AI", "GR"],
  "communalCodes": [],
  "professionCodes": [],
  "workloadPercentageMin": 0,
  "workloadPercentageMax": 100,
  "onlineSince": 30,
  "permanent": null,
  "displayRestricted": false,
  "radiusSearchRequest": { "geoPoint": {"lon": 8.7286, "lat": 47.4989}, "distance": 30 }
}
```

**Datensatz pro Inserat** — mit Abstand der reichhaltigste der drei Portale:

```
id (UUID), stellennummerEgov, stellennummerAvam, externalReference, fingerprint,
status, sourceSystem, createdTime, updatedTime,
reportingObligation, reportToAvam,
jobContent:
  externalUrl                     ← Original-Inserat beim Arbeitgeber
  numberOfJobs
  jobDescriptions[] { languageIsoCode, title, description (Markdown) }
  company   { name, street, houseNumber, postalCode, city, countryIsoCode,
              phone, email, website, surrogate }
  employment{ startDate, endDate, shortEmployment, immediately, permanent,
              workloadPercentageMin, workloadPercentageMax, workForms[] }
  location  { city, postalCode, communalCode, regionCode, cantonCode,
              countryIsoCode, coordinates { lon, lat } }   ← Geo out of the box
  occupations[]  { avamOccupationCode, workExperience, educationCode,
                   qualificationCode }
  languageSkills[] { languageIsoCode, spokenLevel, writtenLevel }
  applyChannel   { emailAddress, phoneNumber, formUrl, rawPostAddress, postAddress }
  publicContact  { salutation, firstName, lastName, phone, email }  ⚠️ VERWERFEN
publication { startDate, endDate, euresDisplay, publicDisplay, companyAnonymous }
```

**Gemessene Volumen (27.07.2026), Zielkantone ZH/SG/TG:**

| Abfrage | Treffer |
|---|---|
| **ZH + SG + TG, 30 Tage** | **19'216** |
| ZH + SG + TG, 7 Tage | 6'021 |
| ZH + SG + TG, 1 Tag | **663** |
| nur ZH, 30 Tage | 13'079 |
| nur SG, 30 Tage | 4'074 |
| nur TG, 30 Tage | 2'056 |
| Ganze CH, 30 Tage (Referenz) | 54'557 |
| kw «Wirtschaftsinformatik», 60 Tage, ZH/SG/TG | 141 |
| kw «Python», 60 Tage, ZH/SG/TG | 164 |
| kw «Business Intelligence», 60 Tage, ZH/SG/TG | 70 |
| kw «Informatiker», 60 Tage, ZH/SG/TG | 74 |
| kw «Data Scientist», 60 Tage, ZH/SG/TG | 20 |

**Daraus folgt die Collection-Strategie:**

- **Täglich:** `onlineSince=1`, `cantonCodes: ["ZH","SG","TG"]` → ~663 Inserate,
  **2 Requests** bei `size=500`. Weit unter dem 10'000er-Limit, kein Slicing.
- **Backfill einmalig:** `onlineSince=7` (6'021 < 10'000) in einem Rutsch. Ein
  30-Tage-Backfill (19'216) **würde** das Limit sprengen und müsste nach Kanton
  gesliced werden — wobei ZH allein mit 13'079 immer noch drüber liegt und eine
  zweite Achse bräuchte (Pensum-Bänder oder `professionCodes`). Den Aufwand
  sparst du dir: mit `onlineSince=7` starten und die Historie ab dann täglich
  wachsen lassen.
- **Kantonsfilter serverseitig** setzen (spart Requests), aber die Rohantwort
  vollständig speichern. Willst du später einen Kanton dazunehmen, ist nur die
  Config zu ändern.

#### Woher kommen die Inserate? (Stichprobe 1'000 Inserate, ZH/SG/TG, 30 Tage)

**job-room aggregiert nicht von jobs.ch — die Inserate werden dort von den
Firmen selbst eingeliefert.** Das Feld `sourceSystem` sagt es direkt:

| `sourceSystem` | Anteil | Bedeutung |
|---|---|---|
| `API` | 69.1% | Arbeitgeber/Vermittler liefern über die offizielle Publikations-API ein (die aus Abschnitt «Zur Ausgangsfrage») — meist automatisch aus ihrem Bewerbermanagement |
| `EXTERN` | 21.0% | Einlieferung aus angebundenen Fremdsystemen |
| `JOBROOM` | 8.2% | direkt im Job-Room-Webformular erfasst |
| `RAV` | 1.7% | von RAV-Mitarbeitenden erfasst |

**Überraschung: nur 1.5% der Inserate sind meldepflichtig** (`reportingObligation
= true`). Die grosse Mehrheit wird **freiwillig** publiziert — Job-Room ist für
Arbeitgeber kostenlos, deshalb nehmen viele es als Zusatzkanal mit. Die
Ausgangsannahme «Job-Room = nur Stellenmeldepflicht» stimmt also nicht.

**Aktualität ist sehr gut:** in der nach Datum sortierten Stichprobe waren 47%
am Abfragetag selbst erstellt, 95.5% innerhalb von zwei Tagen. Kein Altbestand.

**Und ein nützlicher Nebeneffekt:** unter den `externalUrl`-Zielen (dorthin
führt die Bewerbung) stehen **jobs.ch mit 151 und jobup.ch mit 34 von 1'000**.
Dieselben Arbeitgeber publizieren auf beiden Plattformen. Du bekommst also über
job-room einen legitimen Zeiger auf einen erheblichen Teil des
jobs.ch-Bestands — inklusive Inseratstext, den job-room selbst ausliefert —
**ohne jobs.ch je anzufassen**. Das entschärft den Verzicht aus Abschnitt 1
deutlich.

Weitere Top-Ziele: `med-ipersonal.ch` (395), `ipersonal.ch` (51),
`ohws.prospective.ch` (32), `evergreen-hr.ch` (27). 115 Inserate haben gar keine
`externalUrl`.

#### ⚠️ Das grösste Datenqualitätsproblem: Personalvermittler-Flut

| Firma | Anteil an 1'000 Inseraten |
|---|---|
| **MediPersonal** | **395 (39.5%)** |
| iPersonal AG | 51 (5.1%) |
| Jobup | 33 (3.3%) |
| Evergreen Human Resources AG | 31 (3.1%) |
| Work4you AG | 23 (2.3%) |
| **Total `company.surrogate = true`** | **503 (50.3%)** |

**Die Hälfte des Bestands stammt von Personalvermittlern, ein einziger Anbieter
stellt 40%.** Stichproben der MediPersonal-Texte zeigen offensichtlich
LLM-generierte, nach Ort und Beruf durchvariierte Fliesstexte («Beromünster
liegt im Kanton Luzern und bietet Ihnen eine attraktive Arbeitsumgebung…») —
dieselbe Stelle wird für Dutzende Ortschaften dupliziert.

Das ist kein Randproblem, sondern **bestimmt die Architektur mit**: ohne
Gegenmassnahme besteht dein Ranking zur Hälfte aus Vermittler-Rauschen. Deshalb
im Plan vorgesehen:

- `company_is_agency` (aus `company.surrogate`) als hartes, konfigurierbares
  Filter- bzw. Malus-Kriterium
- **Near-Duplicate-Erkennung innerhalb einer Firma**: gleiche Firma + sehr
  ähnlicher Text (MinHash/SimHash über Description-Shingles) → als eine Stelle
  clustern, nicht als 40 Treffer ausspielen
- Cap pro Firma im Digest (z.B. max. 3 Treffer je Arbeitgeber)

**Fazit:** Grün. Öffentliche Behördendaten (SECO/arbeit.swiss), strukturiert,
mit Geokoordinaten und explizitem Ablaufdatum, ohne Anti-Bot-Massnahmen — aber
mit einem realen Rauschproblem, das die Pipeline aktiv behandeln muss.

**Ehrliche Einschränkung, die ins Repo gehört:** Die robots.txt trägt den
Kommentar `# Do not crawl Job Adverts`. Technisch greift die `Disallow`-Regel nur
für `/job-search/`, nicht für `/jobadservice/` — die Absicht des Betreibers ist
aber lesbar. Deshalb: ehrlicher User-Agent mit Kontaktangabe, ≤1 req/s, tägliches
Inkrement statt Vollcrawl, Caching, keine Weiterverbreitung. Das ist der
Unterschied zwischen «respektiert den Betreiber» und «hat die Lücke gefunden».

---

### Bestehende Open-Source-Scraper & Apify-Actors

**Ergebnis: es gibt keine brauchbare Referenz-Implementierung.** Die GitHub-Suche
liefert 42 Treffer für Swiss-Job-Scraper — **alle mit 0 Stars**, und der Grossteil
sind auto-generierte Spiegel-Repos kommerzieller Apify-Actors
(`AbsoluteAnchor/jobs-ch-scraper`, `BuzzGoMax/job-room-ch-jobs-scraper-…`, …).
Kein gepflegtes Community-Projekt, kein Code, von dem man Architektur lernen kann.
Die SECO-nahe Org `alv-ch` enthält nur Angular-Forks, keinen Jobroom-Code.

Nützlich bleiben die **Feldnamen** der Apify-Actors als Vergleich für das eigene
Schema ([ostjob](https://apify.com/santamaria-automations/ostjob-ch-scraper),
[arbeit.swiss](https://apify.com/santamaria-automations/arbeit-swiss-scraper),
beide ~$3/1000 Ergebnisse):

```
id, title, company, location, canton, job_status, employment_type,
workload_min, workload_max, remote_option, description_snippet,
description_full, requirements[], posted_at, expires_at,
source_url, source_platform, apply_url, company_url, search_query, scraped_at
```

Das ist eine vernünftige Basis — mit **einem entscheidenden Unterschied**: diese
Actors extrahieren `contact_firstname`, `contact_lastname`, `contact_email`,
`contact_phone` und sind auf Apify unter **«Lead generation»** kategorisiert.
Genau das ist unter revDSG der problematische Teil und wird hier bewusst
**nicht** übernommen. Das ist ein guter Absatz fürs README.

---

## 2. Quellenvergleich — alle geprüften Optionen

Vor der Festlegung wurden 18 Quellen empirisch geprüft (robots.txt, AGB,
API-Verfügbarkeit, Datentiefe, Volumen). Ergebnis:

### Die vollständige Übersicht

| Quelle | Zugang | Recht | Datentiefe | Volumen | Urteil |
|---|---|---|---|---|---|
| **job-room.ch** | öffentliche JSON-API, kein Key | 🟢 | ★★★★★ Geo, Pensum-Range, Volltext, Ablaufdatum, AVAM-Codes | 19'216 / 30 T (ZH·SG·TG) | **primär** |
| **myjob.ch** | Sitemap + `__PRELOADED_STATE__` | 🟡 | ★★★★ Pensum, `home_office`, Volltext | **> 50'000** | **primär #2 (neu)** |
| **ostjob.ch** | dito, identischer Parser | 🟡 | ★★★★ | 6'119 | sekundär (SG/TG) |
| **zentraljob.ch** | dito, identischer Parser | 🟡 | ★★★★ | 1'720 | optional, ~0 Aufwand |
| ~~jobs.ch~~ | — | 🔴 AGB Ziff. 7 verbietet Scraping wörtlich | — | — | raus |
| ~~jobup.ch~~ / ~~jobscout24.ch~~ | — | 🔴 gleiche JobCloud-AGB | — | — | raus |
| ~~jobagent.ch~~ | — | 🔴 robots.txt: `User-agent: *` → `Disallow: /` | — | — | raus |
| ~~Indeed~~ | Publisher-API seit Jahren geschlossen; Cloudflare-Challenge | 🔴 | — | — | raus |
| ~~LinkedIn~~ | 33 pauschale `Disallow`-Gruppen, harter Bot-Schutz | 🔴 | — | — | raus |
| **Adzuna** | offizielle REST-API, Key gratis nach Registrierung | 🟡 | ★★ **nur Snippet** — aber **Lohndaten** | 2'500 Calls/Monat | nur als Lohn-Benchmark |
| Careerjet | offizielle API, gratis (Affiliate) | 🟡 | ★★ nur Snippet, URL = Tracking-Redirect | Frauenfeld nur 22 Treffer | nein |
| Jooble | API nur auf Anfrage, Web hinter Cloudflare | ⚪ | ? | ? | nein |
| EURES (EU) | `401` — Registrierung nötig | ⚪ | ? | ? | nein |
| swissdevjobs.ch | API-Endpoint **deprecated** («contact us») | ⚪ | — | — | nein |
| opendata.swiss | 271 Arbeitsmarkt-Datensätze, aber **keine Stelleninserate** (nur Statistik) | 🟢 | — | — | nein |
| talent.com / topjobs / jobwinner | Aggregatoren ohne öffentliche API | ⚪ | ★ | — | nein |
| ATS-Systeme (prospective.ch, refline.ch, ostendis.com) | pro Firma eigener Endpoint | 🟢 | ★★★★ | klein je Firma | später, gezielt |

### Die wichtigste Erkenntnis: myjob.ch

**myjob.ch ist technisch identisch mit ostjob.ch** — verifiziert: gleiche
`robots.txt` (inkl. derselben namentlich gesperrten Job-Bots), gleiche
`sitemap-vacancies.xml`-Struktur, gleiches `window.__PRELOADED_STATE__` mit
demselben `vacancyDetails.data`-Objekt. Beide gehören CH Media Classifieds.

Der Unterschied ist die **Reichweite**: ostjob deckt die Ostschweiz mit 6'119
Inseraten ab, **myjob.ch ist das nationale Portal mit über 50'000** (zwei
Sitemap-Dateien, `sitemap-vacancies.xml` + `-2.xml`, die erste allein am
50'000er-Limit). Stichprobe bestätigt Inserate aus der ganzen Schweiz.

**Praktische Konsequenz: ein Adapter, vier Portale.** Der Fetcher/Parser, der
für ostjob gebaut wird, funktioniert mit einem Konfigurationseintrag (Host)
auch für myjob.ch, zentraljob.ch und weitere Portale der Familie. Das ist der
mit Abstand beste Aufwand-Ertrag-Hebel im ganzen Projekt.

### Warum die Aggregatoren ausscheiden

Adzuna, Careerjet und Jooble haben zwar legitime, offizielle APIs — aber alle
drei liefern **nur einen Textausschnitt der Stellenbeschreibung**. Adzuna sagt es
in der Doku selbst: *«we currently only provide a snipped of the job
description»*. Für ein embedding-basiertes Matching ist das der K.-o.: der
semantische Score lebt vom Volltext. Careerjet gibt zusätzlich statt der echten
Inserats-URL nur einen `jobviewtrack.com`-Redirect zurück.

Dazu kommen Nutzungsauflagen, die zu einem privaten Tool schlecht passen:
Adzuna verlangt bei Veröffentlichung ein «Jobs by Adzuna»-Logo, begrenzt auf
250 Calls/Tag und behält sich für Nicht-Publisher-Nutzung eine **14-Tage-
Testfrist** vor, nach der eine Lizenzvereinbarung nötig werden kann. Careerjet
läuft über ein Affiliate-Programm.

**Eine Ausnahme lohnt sich:** Adzuna liefert als einzige geprüfte Quelle
**Lohndaten** (`salary_min`/`salary_max` und die «Jobsworth»-Schätzung). Genau
das Feld, das job-room, ostjob und myjob alle nicht haben. Als *optionaler
Benchmark* in Milestone 5 — nicht als Inseratsquelle — ist Adzuna damit die
naheliegendste Antwort auf die offene Lohn-Frage. Registrierung müsstest du
selbst vornehmen (kostenlos).

---

## 2b. Empfohlener Ansatz

| | Quelle | Rolle | Methode |
|---|---|---|---|
| **Primär A** | **job-room.ch** | Beste Datentiefe, Geo + Pensum + Ablaufdatum | Öffentliche `jobadservice`-JSON-API, tägliches Inkrement `onlineSince=1` |
| **Primär B** | **myjob.ch** | Grösste Abdeckung (>50'000), auch ZH | `sitemap-vacancies*.xml` → Detailseiten → `__PRELOADED_STATE__` |
| **Sekundär** | **ostjob.ch** | Ostschweizer Arbeitgeber (SG/TG) | identischer Adapter, nur anderer Host |
| **Optional** | zentraljob.ch | falls Radius je erweitert wird | identischer Adapter |
| **Benchmark** | Adzuna API | **nur** Lohn-Schätzungen, keine Inserate | offizielle API, gratis, du registrierst |
| **Raus** | ~~jobs.ch~~, ~~jobup~~, ~~jobscout24~~, ~~jobagent~~, ~~Indeed~~, ~~LinkedIn~~ | — | rechtlich untersagt bzw. technisch verriegelt |

Die zwei Primärquellen ergänzen sich sauber: job-room hat die **bessere
Struktur** (Koordinaten, Pensum-Range, explizites Ablaufdatum), myjob die
**grössere Abdeckung**. Die Überschneidung wird über das ohnehin geplante
Cross-Portal-Dedup abgefangen — und liefert als Nebeneffekt ein gutes
Qualitätssignal: Inserate, die auf beiden Portalen stehen, sind verlässlicher
als Einzelvorkommen.

**Zielkantone: ZH, SG, TG.** job-room deckt sie mit 19'216 Inseraten pro 30 Tage
ab (ZH 13'079 · SG 4'074 · TG 2'056). myjob.ch bringt landesweit über 50'000
Inserate dazu — auch für ZH, wo ostjob nichts beiträgt. ostjob ergänzt SG/TG um
regionale Arbeitgeber. Alle drei über zwei Adapter (einer für job-room, einer
für die CH-Media-Familie).

### Zwei Suchprofile, eine Pipeline

Die Pipeline bedient **zwei Personen mit unterschiedlichen Profilen** auf
demselben Datenbestand. Das ist kein Sonderfall, sondern wird zum
Architekturprinzip: Collection und Parsing sind profil-unabhängig, erst Schicht 3
(Scoring) und Schicht 4 (Output) werden pro Profil ausgeführt.

| | **Profil A — Lucas** | **Profil B — Kollegin** |
|---|---|---|
| Feld | Wirtschaftsinformatik / Data Science | KV Dienstleistung & Administration (EFZ 2024) |
| Ort | Zürich / Winterthur | Frauenfeld, Radius ~25 km |
| Zeithorizont | Bewerbung in ~1 Jahr | aktiv suchend |
| Muss | Pensum, Tech-Stack-Fit | **Gleitzeit**, Bürostelle |
| Ausschluss | — | **direkter Kundenkontakt / Frontdesk / Empfang** |
| Kontext | ZHAW, Data-Science-Fokus | aktuell Frontdesk Immobilien; will ins Backoffice |

**Gemessenes Angebot für Profil B** (job-room, 30 Tage):

| Abfrage | Treffer |
|---|---|
| Radius 25 km um Frauenfeld, alle Stellen | 4'532 |
| kw «Kaufmann/Kauffrau», TG/ZH/SG | 1'059 |
| kw «Administration», TG/ZH/SG | 539 |
| kw «Sachbearbeiter», TG/ZH/SG | 403 |
| kw «Sachbearbeiter», 25 km Frauenfeld | 87 |
| kw «Administration», 25 km Frauenfeld | 74 |
| kw «Backoffice», TG/ZH/SG | 61 |
| kw «Treuhand», 25 km Frauenfeld | 28 |

Ausreichend Substanz — Profil B hat sogar mehr Treffer in der Region als
Profil A. Die Quelle taugt für beide.

**Das macht zwei neue Konzepte nötig, die im ursprünglichen Entwurf fehlten:**

1. **Ausschlusskriterien (negative Regeln).** Profil B will *nicht* an den
   Frontdesk. Ein reines «je ähnlicher, desto besser» findet aber genau die
   Empfangs- und Schalterstellen, weil sie ihrer bisherigen Tätigkeit am
   ähnlichsten sind. Nötig ist eine `exclude`-Liste, die den Score aktiv drückt:
   *Empfang, Reception, Front Office, Front Desk, Schalter, Kundenkontakt,
   Kundenempfang, Detailhandel, Verkaufsberatung, Filiale, Call Center*. Ebenso
   auf der Positivseite: *Backoffice, Sachbearbeitung, Innendienst,
   Auftragsabwicklung, Disposition, Treuhand, Buchhaltung, Personaladministration*.
2. **Profile als Konfiguration, nicht als Code.** Ein Profil ist eine
   YAML-Datei mit CV-Text, Muss-/Wunsch-/Ausschluss-Kriterien, Ort/Radius,
   Gewichten. `pipeline score --profile lucas` bzw. `--profile kollegin`,
   `pipeline digest --profile …`. Ein drittes Profil hinzuzufügen kostet dann
   eine Datei.

**Datenschutz-Hinweis:** Die Profildaten deiner Kollegin (Ausbildung, aktueller
Arbeitgeber, Wünsche) sind Personendaten einer Drittperson. Sie gehören in
`profiles/`, das per `.gitignore` ausgeschlossen wird — im öffentlichen Repo
liegt nur ein `profiles/example.yaml` mit erfundenen Werten. Für das
Portfolio-Repo ist das ohnehin die bessere Variante.

**Erwarteter Ertrag nach fachlicher Filterung:** Profil A grob **150–300**,
Profil B grob **200–400** relevante Inserate pro 60-Tage-Fenster — beides klein
genug, dass die Embedding-Kosten vernachlässigbar bleiben.

---

## 3. Wiederverwendung aus `~/zhaw/DSP`

Geprüft. **Deine Einschätzung stimmt für den Scraper — aber der Datensatz ist ein
echter Fund.**

### Was dort liegt

| Pfad | Was es ist |
|---|---|
| `DSP/DS_project/` | Scraper für **berufsberatung.ch** (nicht für Stellenportale). `requests` + BeautifulSoup, MongoDB, Rate-Limiting mit Jitter |
| `DSP/cv/swiss-cv-generator/` | Generator für **synthetische** Schweizer CVs: BFS-Demografie + Berufsdaten + OpenAI, Export via ReportLab/WeasyPrint, MongoDB |
| `DSP/cv/swiss-cv-generator/data/CV_DATA.cv_berufsberatung.json` | **9.3 MB, 1'851 Schweizer Berufe** — das eigentlich Wertvolle |

### 🟢 Übernehmen: der Berufe-Datensatz

Struktur pro Beruf (verifiziert am File, **nicht** wie im dortigen README
beschrieben — das ist veraltet):

```
title, description, url, job_id
categories { berufsfelder, branchen, swissdoc, bildungstypen, aktualisiert }
taetigkeiten { beschreibung, kategorien{<Kategorie>: [Tätigkeiten…]}, summary_stats }
voraussetzungen { vorbildung[], anforderungen[],
                  kategorisierte_anforderungen { fachliche_faehigkeiten[],
                    persoenliche_eigenschaften[], physische_anforderungen[] } }
weiterbildung { career_progression[{level, type, options[]}], fachhochschule, … }
weitere_informationen { verwandte_berufe[{title, url, job_id}], adressen[], externe_links[] }
data_completeness { completeness_score, … }
```

**65 Berufe im Berufsfeld «Informatik»** — u.a. Data Scientist, Dateningenieur/in,
Cloud Engineer, Artificial Intelligence Specialist, DevOps Engineer,
ICT-Requirements-Engineer, ICT-Architekt/in, Informatiker/in FH/HF/UNI.
15 Berufsfelder insgesamt.

Konkret nutzbar:

1. **Klassifikations-Taxonomie (M5).** `categories.berufsfelder` + `swissdoc`
   liefern lesbare, offizielle Schweizer Labels für die HDBSCAN-Cluster. Statt
   «Cluster 7» steht im Digest «Informatik → Data Science». Ergänzt sich mit den
   `avamOccupationCode`s aus job-room — zwei unabhängige Klassifikationen, die
   sich gegenseitig plausibilisieren.
2. **Deutschsprachiges Seed-Vokabular (M5).** `taetigkeiten.kategorien` ist pro
   Beruf sauber gegliedert — beim Data Scientist z.B. *Datenbewirtschaftung /
   Datenanalyse / Algorithmen / Systeme und Infrastruktur*. Das ist genau die
   Sprache, in der Schweizer Inserate geschrieben sind, und deutlich besser als
   eine selbst zusammengesuchte englische Keyword-Liste.
3. **Nachbar-Rollen-Graph (M5/M6).** `verwandte_berufe` erlaubt «angrenzende
   Rollen, die du vielleicht nicht auf dem Schirm hattest» — ein hübsches
   Feature, das praktisch nichts kostet, weil die Daten schon da sind.
4. **Gap-Analyse (M5).** `weiterbildung.career_progression` gibt die typischen
   nächsten Schritte pro Beruf — genau der Kontext, der eine Skill-Lücke von
   «fehlt dir» zu «lohnt sich für dich» macht.

**Ehrliche Einschränkungen:**

- `fachliche_faehigkeiten` ist **verrauscht**: 356 verschiedene Freitext-Strings
  mit Dubletten in unterschiedlicher Schreibweise («technisches Verständnis» in
  fünf Varianten) — und beim Data Scientist schlicht **leer**. Das ist ein
  Seed, keine fertige Taxonomie; einmalige Bereinigung nötig.
- Es enthält **keine Tech-Stack-Begriffe** (Python, SQL, dbt, Airflow …). Das
  ist eine Berufs-, keine Werkzeug-Taxonomie. Das Tech-Keyword-Dictionary musst
  du weiterhin selbst pflegen.
- `weitere_informationen.adressen` enthält Telefonnummern von Bildungsinstituten.
  Beim Import mit derselben Allowlist behandeln wie die Portaldaten.

**Vorgehen:** Datei einmalig nach `data/ref/berufe_ch.json` kopieren, mit einem
Skript auf die vier gebrauchten Felder reduzieren (`title`, `categories`,
`taetigkeiten.kategorien`, `verwandte_berufe`, `career_progression`) und als
schlanke Referenztabelle in SQLite laden. **Kein MongoDB nötig** — die Datei ist
statisch.

### 🟡 Übernehmen: einzelne Code-Muster

Aus `DS_project/job_scraper.py` bzw. `scraper/clean_scraper.py`:
`sleep_with_jitter()`, die Konstanten `REQUEST_TIMEOUT_SECONDS` / `RETRY_COUNT` /
`SLEEP_BETWEEN_REQUESTS_SECONDS`, die Retry-Schleife und `normalize_text()`.
Klein, aber du hast sie schon geschrieben und sie funktionieren. Portierung von
`requests` auf `httpx` ist trivial.

Für **M7 (Anschreiben)** später relevant:
`src/generation/openai_client.py`, `src/generation/prompts.py`,
`src/export/pdf_renderer_reportlab.py`, `src/export/pdf_templates.py`.

### 🔴 Nicht übernehmen

- **Der Scraper selbst.** Er zielt auf berufsberatung.ch — statische
  Berufsbeschreibungen, andere DOM-Struktur, andere Semantik. Mit
  Stelleninseraten hat er nichts gemein ausser der Sprache. Neu schreiben.
- **MongoDB + docker-compose.** Bewusste Abweichung: die neuen Daten sind
  relational (Inserate ↔ Quellen ↔ Scores ↔ Cluster), das Volumen ist klein
  (<100k Zeilen), und SQLite ist ein einzelnes File ohne laufenden Dienst — für
  ein Portfolio-Repo, das jemand in zwei Minuten klonen und starten können soll,
  klar die bessere Wahl. Für Ad-hoc-Analysen liest DuckDB die SQLite-Datei
  direkt. Falls du MongoDB aus Lerngründen willst: sag es, dann plane ich um.
- **Der CV-Generator.** Erzeugt *synthetische* CVs für ein anderes Erkenntnis-
  ziel. Hier geht es um deinen echten CV — nur die PDF-/Prompt-Bausteine sind
  in M7 nützlich.

---

## 4. Architektur

```
┌─ 1 COLLECTION ──────────────────────────────────────────────────┐
│  JobRoomFetcher      POST /jobadservice/api/…/_search           │
│                      onlineSince=1, cantons ZH/SG/TG, size=500  │
│  ChMediaFetcher      EIN Adapter, N Hosts (config):             │
│    myjob.ch · ostjob.ch · zentraljob.ch                         │
│                      sitemap-vacancies*.xml → lastmod-Diff       │
│                      → GET /job/<slug>/<id> → __PRELOADED_STATE__│
│  gemeinsam: RateLimiter(1 req/s) · robots.txt-Check (protego)   │
│             ETag/lastmod-Cache · Retry mit Backoff              │
│  ↓ schreibt roh & unverändert                                    │
│  data/raw/<portal>/<YYYY-MM-DD>/<id>.json.gz                    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 2 PARSING / NORMALISIERUNG ────────────────────────────────────┐
│  Adapter je Portal → gemeinsames Pydantic-Schema `JobPosting`   │
│  PII-Allowlist (hart, nicht Denylist)  ← revDSG                 │
│  Geocoding PLZ → lat/lon (nur ostjob; job-room liefert direkt)  │
│  Dedup: exakt (portal, source_id) → Cluster (rapidfuzz)         │
│  ↓ SQLite: jobs · job_sources · runs                            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 3 ENRICHMENT / MATCHING ───────────────────────────────────────┐
│  Embeddings (Gemini) für Beschreibung + CV → Cosine = semantic  │
│  Regelscore: Pensum · Radius · Homeoffice · Tech-Keywords       │
│  Gewichtete Summe → final_score                                 │
│  HDBSCAN über Embeddings → Job-Typ-Cluster                      │
│  Cluster-Labeling via Berufsfelder aus berufe_ch.json (DSP)     │
│  Gap-Analyse: Skill-Häufigkeit im Cluster vs. CV-Skills         │
│  läuft PRO PROFIL (lucas | kollegin) — Embeddings geteilt       │
│  ↓ SQLite: embeddings · scores(profile) · clusters · berufe     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─ 4 OUTPUT ──────────────────────────────────────────────────────┐
│  M4  Täglicher Digest (Jinja2 → Markdown/HTML), Top-N + Deltas  │
│  M6  Streamlit-Dashboard: Filter, Sortierung, Feedback-Erfassung│
│  M7  Anschreiben-Agent (opt-in, nie automatisch versendet)      │
└─────────────────────────────────────────────────────────────────┘
```

### Schicht 1 — Collection

Ein `Fetcher`-Protokoll, zwei Implementierungen. Gemeinsame Basis:

- **Höflichkeit als Code, nicht als Vorsatz:** zentraler `RateLimiter`
  (Token-Bucket, Default 1 req/s pro Host), `protego` zum Auswerten der
  robots.txt **zur Laufzeit** (nicht einmalig beim Schreiben des Codes) — wenn
  ostjob morgen unseren UA sperrt, hält die Pipeline von selbst an.
- **User-Agent mit Kontakt:**
  `swiss-job-pipeline/0.1 (persoenliches Bewerbungsprojekt; +https://github.com/<user>/<repo>)`
  — identifizierbar, nicht getarnt. Kein Browser-UA-Spoofing, keine
  Residential-Proxies, keine CAPTCHA-Umgehung.
- **Rohdaten unverändert** nach `data/raw/` (gzip). Parser dürfen jederzeit neu
  laufen, ohne die Portale erneut zu belasten. Das ist gleichzeitig die
  Grundlage für reproduzierbare Tests.
- **Inkrementell:** job-room über `onlineSince=1` mit
  `cantonCodes: ["ZH","SG","TG"]` (~663 Inserate, 2 Requests/Tag); ostjob über
  `lastmod`-Diff gegen den letzten Sitemap-Snapshot, danach lokal auf PLZ in
  SG/TG gefiltert. Nur geänderte/neue IDs werden geholt.
- **`410 Gone` (ostjob) bzw. `publication.endDate` (job-room)** setzen
  `jobs.status = 'expired'` statt den Datensatz zu löschen — die Historie ist
  für die Marktanalyse wertvoll.

### Schicht 2 — Parsing / Normalisierung

Gemeinsames Schema (Pydantic v2), portal-agnostisch:

```python
class JobPosting(BaseModel):
    # Identität
    portal: Literal["job_room", "ostjob"]
    source_id: str                  # UUID bzw. numerische ID
    source_url: HttpUrl
    cluster_id: str | None          # Dedup über Portale hinweg
    # Kern
    title: str
    company_name: str
    company_is_agency: bool         # job-room: company.surrogate
    description_md: str
    description_truncated: bool     # ostjob-iframe-Fälle
    # Ort
    city: str; postal_code: str | None
    canton: str | None; lat: float | None; lon: float | None
    # Konditionen
    workload_min: int | None; workload_max: int | None
    is_permanent: bool | None; start_date: date | None
    home_office: bool | None
    salary_min: int | None          # praktisch immer None — siehe Risiken
    # Zeit
    posted_at: datetime; expires_at: datetime | None
    status: Literal["active", "expired"]
    # Bewerbung
    apply_url: HttpUrl | None       # externalUrl / formUrl / url_application
    # Klassifikation
    occupation_codes: list[str]     # AVAM (nur job-room)
    categories: list[str]
    languages: list[str]
    # Meta
    first_seen_at: datetime; last_seen_at: datetime; raw_path: Path
```

**PII-Handling (revDSG) — als Allowlist, nicht als Denylist.** Der Parser
konstruiert `JobPosting` aus einer expliziten Feldliste. Alles, was nicht
aufgeführt ist, landet gar nicht erst im Modell. Konkret **nie übernommen**:

| Quelle | Feld |
|---|---|
| job-room | `jobContent.publicContact` (`salutation`, `firstName`, `lastName`, `phone`, `email`) |
| job-room | `jobContent.applyChannel.emailAddress`, `.phoneNumber` |
| job-room | `jobContent.company.email`, `.phone` |
| ostjob | `vacancyDetails.data.contact` (Name + Mail + Telefon als HTML-Blob) |

Behalten wird die **Bewerbungs-URL** (`formUrl` / `url_application` /
`externalUrl`) — dort bewirbst du dich ohnehin über ein Formular, ohne dass ein
Personenname in deiner DB liegt. Firmenname und Firmenadresse bleiben (juristische
Person). Ein Test in der Suite prüft, dass in keiner Spalte der DB ein
`@`-Zeichen oder ein `+41`-Muster in Kontaktfeldern auftaucht.

**Deduplizierung** in zwei Stufen:

1. **Exakt:** `(portal, source_id)` ist der Primärschlüssel. Innerhalb von
   job-room zusätzlich `stellennummerEgov` und das von der API gelieferte
   `fingerprint`-Feld (falls gesetzt).
2. **Cross-Portal (fuzzy):** Blocking auf normalisierter PLZ bzw. Gemeinde, um
   den Vergleichsraum klein zu halten. Innerhalb eines Blocks Score aus
   `rapidfuzz`:
   - `token_set_ratio` auf normalisiertem Titel (Pensumangaben, `(m/w/d)`,
     Geschlechtsdoppelformen und Klammerzusätze vorher strippen) — Gewicht 0.45
   - `token_set_ratio` auf normalisiertem Firmennamen (Rechtsform `AG`/`GmbH`/
     `SA` entfernen) — Gewicht 0.35
   - Ortsgleichheit — Gewicht 0.20
   - Schwelle ~0.87 → gemeinsame `cluster_id`
   - **Starkes Zusatzsignal:** gleicher Host+Pfad in `apply_url`. Viele
     ostjob-Inserate zeigen auf dieselbe Arbeitgeber-URL wie der
     job-room-Eintrag — das ist praktisch ein Beweis und darf die Fuzzy-Schwelle
     überstimmen.

   Bei einem Cluster gewinnt job-room als kanonische Quelle (reichere Felder);
   ostjob-spezifische Felder wie `home_office` werden hinzugemerged.
3. **Near-Duplicates innerhalb einer Firma (neu, wegen der Vermittler-Flut).**
   MinHash über Shingles der Description, gruppiert je `company_name`. Ein
   Vermittler, der dieselbe Stelle für 40 Ortschaften ausschreibt, wird zu
   **einem** Eintrag mit einer Ortsliste zusammengefasst. Ohne diesen Schritt
   ist der Digest unbrauchbar — MediPersonal allein stellt 39.5% des Bestands.

**Geocoding:** job-room liefert `coordinates` direkt. Für ostjob wird ein
lokales PLZ→lat/lon-Mapping benötigt (amtliches Ortschaftenverzeichnis von
swisstopo/BFS, einmalig als CSV ins Repo) — kein Online-Geocoder, keine
Rate-Limits, reproduzierbar.

### Schicht 3 — Enrichment / Matching

Diese Schicht läuft **pro Profil** (`--profile lucas` / `--profile kollegin`);
Embeddings der Inserate werden dabei geteilt, nur CV-Vektor, Regeln und Gewichte
unterscheiden sich. Ein zweites Profil kostet also fast keine zusätzlichen
Embedding-Kosten.

**Semantischer Score.** Gemini Embedding (`gemini-embedding-001`) über
`Titel + "\n\n" + description_md` (auf ~2000 Tokens gekürzt) und über den CV.
`task_type="SEMANTIC_SIMILARITY"` für beide Seiten, da es ein symmetrischer
Vergleich ist. `output_dimensionality=768` reicht hier und hält die DB klein —
bei Matryoshka-Truncation muss der Vektor **neu L2-normalisiert** werden, sonst
sind die Cosine-Werte verzerrt. Embeddings werden über einen Hash des
Eingabetexts gecacht; ein Inserat wird nie zweimal eingebettet.

Zusätzlich lohnt sich innerhalb eines Profils ein **Vektor je Zielrolle** (bei
Lucas z.B. «Data Engineering» / «BI/Analytics» / «Software Engineering», bei der
Kollegin «Sachbearbeitung Innendienst» / «Treuhand/Buchhaltung» /
«Personaladministration») statt eines einzigen Durchschnitts-CVs — der Max-Score
über die Zielrollen trennt deutlich besser als ein gemittelter Vektor. Für
Profil B ist das besonders wichtig, weil ihr bisheriger CV stark nach Frontdesk
klingt und ein naiver CV-Vektor genau die unerwünschten Stellen anziehen würde.
Die Zielrollen beschreiben den **Wunschzustand**, nicht den Ist-Zustand.

**Regelbasierter Score** (jedes Kriterium → 0…1, dann gewichtet):

| Kriterium | Quelle | Bemerkung |
|---|---|---|
| Pensum | `workload_min/max` | Overlap mit Wunschbereich |
| Ort / Radius | Haversine auf lat/lon | weiche Abstufung, nicht harter Cut |
| Homeoffice | ostjob `home_office`; job-room `workForms` + Keyword-Regex | |
| **Gleitzeit** | **nur** Keyword-Regex über die Beschreibung | kein strukturiertes Feld — siehe Warnung unten |
| Positiv-Keywords | Profil-Liste (Tech-Stack bzw. Backoffice-Begriffe) | mit Aliassen (`Postgres`≈`PostgreSQL`) |
| **Ausschluss-Keywords** | Profil-Liste | **negativ gewichtet**, drückt den Score aktiv |
| Mindestlohn | **kein Feld verfügbar** | siehe Risiken |
| Aktualität | `posted_at` | leichter Decay |
| Personaldienstleister | `company.surrogate` | Malus; bei 50% Anteil essenziell |

`final_score = w_sem · semantic + Σ wᵢ · ruleᵢ − Σ vⱼ · excludeⱼ`. Alle
Teilscores werden mitgespeichert, damit im Digest **begründbar** ist, warum ein
Job oben oder unten steht. Gewichte pro Profil in `profiles/<name>.yaml`, nicht
im Code.

> **⚠️ Wichtiger Realitäts-Check zu «Gleitzeit».** Gemessen an 1'000 Inseraten:
> «Gleitzeit» o.ä. kommt in **1.2%** der Texte vor, «flexible Arbeitszeit» in
> 7.9%, «Homeoffice/Remote» in 4.8%. Das strukturierte Feld `workForms` ist
> praktisch immer leer (in 1'000 Inseraten genau **1×** `HOME_WORK`).
>
> Für Profil B ist Gleitzeit ein Hauptkriterium — es lässt sich aus den
> Portaldaten aber **nicht zuverlässig herauslesen**. Ein harter Filter darauf
> würde 98% der Stellen wegwerfen, die meisten davon zu Unrecht.
>
> **Konsequenz:** Gleitzeit wird als **Bonus** geführt, nie als Filter. Wo es
> explizit dasteht, gibt es Punkte und der Digest zitiert die Fundstelle; wo
> nicht, bleibt die Stelle im Rennen und die Frage wandert ins
> Vorstellungsgespräch. Zusätzlich ein schwaches Proxy-Signal: Branchen mit
> typischerweise geregelter Gleitzeit (öffentliche Verwaltung, Versicherung,
> Treuhand, Industrie-Backoffice) leicht bevorzugen. Das ist eine Heuristik und
> wird im Output als solche gekennzeichnet.

**Clustering.** HDBSCAN (`sklearn.cluster.HDBSCAN`, seit scikit-learn 1.3
eingebaut — keine separate `hdbscan`-Abhängigkeit nötig) auf den Embeddings.
Wichtig: HDBSCAN direkt auf 768 Dimensionen funktioniert schlecht — vorher mit
UMAP auf ~10–15 Dimensionen reduzieren.

**Cluster-Labeling zweigleisig:** (a) häufigste TF-IDF-Terme je Cluster, (b)
Nearest-Neighbour gegen die Embeddings der 65 Informatik-Berufe aus
`berufe_ch.json` → offizielles Berufsfeld + Berufsbezeichnung als lesbares
Label. (b) macht den Unterschied zwischen «Cluster 7: python, daten, cloud» und
«Informatik → Dateningenieur/in». Ergebnis: eine Karte der Job-Typen im
Schweizer WI/DS-Markt — der Teil, der im Portfolio am meisten hermacht.

**Gap-Analyse.** Skill-Extraktion per Dictionary + Regex (robuster und erklärbarer
als LLM-Extraktion, und kostenlos). Das Dictionary hat zwei Quellen: das
deutschsprachige Tätigkeits-Vokabular aus `berufe_ch.json` (bereinigt) plus eine
selbst gepflegte Tech-Stack-Liste mit Aliassen. Pro Cluster: Häufigkeit der
Skills vs. deine CV-Skills → «in 68% der Inserate deines Top-Clusters gefordert,
fehlt dir: dbt, Airflow, Snowflake». Über `career_progression` aus demselben
Datensatz lässt sich ergänzen, welcher Ausbildungs-/Karriereschritt dazu
typischerweise passt. Konkret verwertbar für die Vorbereitung im kommenden Jahr.

### Schicht 4 — Output

**M4 — Täglicher Digest.** Jinja2 → Markdown + HTML. Top-N nach `final_score`,
gruppiert nach Cluster, mit Score-Breakdown pro Eintrag und einem
Delta-Abschnitt («neu seit gestern», «läuft in 3 Tagen ab»). Ausgabe als Datei;
Versand per Mail bewusst erst später und nur an dich selbst.

**M6 — Streamlit-Dashboard.** Filter (Kanton, Radius, Pensum, Cluster,
Score-Schwelle), Sortierung, Cluster-Scatter (UMAP-2D), Skill-Gap-Ansicht. Ein
Daumen-hoch/runter je Inserat schreibt in eine `feedback`-Tabelle — das ist
später das Trainingsmaterial, um die Gewichte zu kalibrieren statt zu raten.

**M7 — Anschreiben-Agent (opt-in).** Erzeugt pro Top-Treffer einen Entwurf aus
CV + Inserat. Klar abgegrenzt: schreibt **nur Dateien** nach `drafts/`,
versendet nichts, bewirbt sich nirgends. Jeder Entwurf wird von dir gelesen und
überarbeitet.

### Tech-Stack

| Zweck | Wahl | Warum |
|---|---|---|
| HTTP | **httpx** | HTTP/2, sauberes Timeout-Handling, sync+async, `respx` zum Testen |
| Retry | **tenacity** | Exponential Backoff deklarativ |
| robots.txt | **protego** | Googles Parsing-Semantik, korrekt bei Wildcards — `urllib.robotparser` ist es nicht |
| HTML | **selectolax** | ~10× schneller als BeautifulSoup; für ostjob genügt Regex+`json.loads` auf `__PRELOADED_STATE__` |
| JSON-LD | **extruct** | falls doch mal HTML-Fallback gebraucht wird |
| Schema | **pydantic v2** | Validierung + Serialisierung, `model_config` für strikte Felder |
| Storage | **SQLite** (`sqlite-utils`) | Ein File, versionierbar, kein Server. Bei <100k Zeilen völlig ausreichend. Bewusst statt MongoDB (siehe Abschnitt 3) |
| Analyse | **DuckDB** | liest SQLite und Parquet direkt, für Ad-hoc-Auswertungen und Notebooks |
| Fuzzy-Match | **rapidfuzz** | C++-schnell, `token_set_ratio` genau richtig für Titel |
| Embeddings | **google-genai** | `gemini-embedding-001`, wie gewünscht |
| Fallback | **sentence-transformers** (`paraphrase-multilingual-mpnet-base-v2`) | offline, kostenlos, deutschsprachig brauchbar — hält die Pipeline testbar ohne API-Key |
| Vektoren | **numpy** | bei wenigen tausend Inseraten ist eine Vektor-DB Overkill; `np.dot` auf normalisierten Vektoren genügt |
| Clustering | **scikit-learn** (HDBSCAN) + **umap-learn** | HDBSCAN ist seit 1.3 in sklearn integriert |
| Templates | **Jinja2** | Digest |
| UI | **Streamlit** | schnellster Weg von DataFrame zu Dashboard |
| CLI | **typer** | `pipeline fetch` / `parse` / `score` / `digest` |
| Config | **pydantic-settings** + `config.yaml` | Gewichte und Filter ohne Code-Änderung |
| Logging | **structlog** | strukturierte Logs, gut für die Run-Historie |
| Tests | **pytest** + **respx** | HTTP gemockt gegen echte, eingecheckte Rohdaten-Fixtures |
| Qualität | **ruff** + **mypy** | im Portfolio-Repo sichtbar |
| Scheduling | **cron** lokal, später **GitHub Actions** | Achtung: GH-Runner-IPs sind US-basiert — job-room antwortet darauf, aber lokal ist freundlicher |

Paketverwaltung mit **uv** (`pyproject.toml`, `uv.lock`) — schnell und
reproduzierbar.

### Repo-Struktur

```
bewerbung/
├─ PLAN.md · README.md · LICENSE
├─ pyproject.toml · uv.lock · config.yaml · .env.example
├─ profiles/     example.yaml (eingecheckt)
│                lucas.yaml · kollegin.yaml   (gitignored — Personendaten)
├─ src/jobpipe/
│  ├─ collect/   base.py · job_room.py · ostjob.py · ratelimit.py · robots.py
│  ├─ parse/     schema.py · job_room.py · ostjob.py · pii.py · dedup.py · geo.py
│  ├─ enrich/    embed.py · rules.py · score.py · cluster.py · skills.py
│  ├─ output/    digest.py · dashboard.py · letters.py
│  ├─ store/     db.py · migrations/
│  └─ cli.py
├─ data/         raw/ (gitignored) · jobs.db (gitignored)
│                ref/plz_coords.csv          (swisstopo/BFS)
│                ref/berufe_ch.json          (reduziert aus ~/zhaw/DSP)
│                ref/tech_skills.yaml        (selbst gepflegt)
├─ tests/        fixtures/ (echte Rohdaten-Samples) · test_*.py
└─ notebooks/    01_explore.ipynb · 02_clusters.ipynb
```

---

## 5. Offene Fragen und Risiken

### Rechtlich / ethisch

| Risiko | Einschätzung | Umgang |
|---|---|---|
| job-room robots.txt sagt `# Do not crawl Job Adverts` | Die `Disallow`-Regel deckt nur `/job-search/`, nicht `/jobadservice/`. Die Absicht ist trotzdem lesbar | Ehrlicher UA mit Kontakt, ≤1 req/s, tägliches Inkrement, Caching, keine Weiterverbreitung. Im README offen benennen statt verschweigen |
| ostjob sperrt Job-Bots namentlich | Aggregatoren unerwünscht; unser UA ist (noch) nicht gelistet | robots.txt bei **jedem Lauf** prüfen. Wenn unser UA je gesperrt wird: Fetcher stoppt automatisch |
| ostjob AGB 13.1 «Verwertung durch Dritte» | Zielt auf 1:1-Weiterverwertung, nicht auf private Analyse | Keine Volltexte im Repo, keine öffentliche Publikation von Inseratsinhalten. Repo enthält Code, nicht Daten |
| revDSG | `publicContact` / `contact` sind klar Personendaten | Allowlist-Parser + Test, der die DB auf Kontaktmuster prüft |
| Portfolio-Sichtbarkeit | Das Repo ist öffentlich und trägt deinen Namen | README beginnt mit dem Abschnitt «Was dieses Projekt bewusst nicht tut» — inkl. der jobs.ch-Entscheidung. Das ist ein Feature, kein Disclaimer |

### Technisch

| Risiko | Auswirkung | Mitigation |
|---|---|---|
| **Kein Lohnfeld auf irgendeinem Portal** | Dein Kriterium «Mindestlohn» ist mit Portaldaten **nicht erfüllbar** | Drei Optionen, in dieser Reihenfolge: (a) Regex auf explizite Lohnangaben im Text — trifft geschätzt <5%; (b) externer Benchmark über den [Salarium-Lohnrechner des BFS](https://www.gate.bfs.admin.ch/salarium/) nach Beruf/Region/Alter als *erwarteter* Lohn; (c) Kriterium fallen lassen. **Empfehlung: (b) als Schätzung, klar als solche gekennzeichnet.** Nicht so tun, als wären es echte Daten |
| **«Gleitzeit» ist nirgends strukturiert** — gemessen nur **1.2%** der Texte, `workForms` praktisch immer leer | Für Profil B ein Hauptkriterium, das die Daten nicht hergeben | Nur als Bonus, nie als Filter. Fundstelle im Digest zitieren. Branchen-Proxy als gekennzeichnete Heuristik. Ehrlich im Output benennen |
| **50% Personalvermittler, MediPersonal allein 39.5%**, teils LLM-generierte Massentexte | Ohne Gegenmassnahme besteht das Ranking zur Hälfte aus Rauschen | `company_is_agency`-Malus + MinHash-Near-Dedup je Firma + Cap pro Arbeitgeber im Digest. **In M2 einzuplanen, nicht später** |
| Profil B: CV klingt nach Frontdesk, gesucht ist Backoffice | Naives CV-Embedding zieht genau die unerwünschten Stellen an | Zielrollen-Vektoren beschreiben den Wunschzustand; zusätzlich negativ gewichtete Ausschluss-Keywords |
| Personendaten einer Drittperson (Kollegin) im Repo | Wäre im öffentlichen Portfolio-Repo ein Eigentor | `profiles/*.yaml` ausser `example.yaml` in `.gitignore`; Test prüft, dass keine echten Profile eingecheckt sind |
| `jobadservice` ist eine **undokumentierte interne API** | Kann sich ohne Ankündigung ändern oder Auth bekommen | Contract-Tests gegen eingecheckte Fixtures; Fetcher schlägt laut fehl statt still leer zu laufen. Rohdaten bleiben erhalten |
| 10'000er Result-Window | Backfill über 30 Tage (19'216 für ZH/SG/TG) nicht in einem Rutsch möglich | Backfill mit `onlineSince=7` (6'021), danach tägliches Inkrement (663). Kein Slicing nötig |
| Berufsdatensatz aus DSP ist Stand 11/2025 | Berufsbezeichnungen veralten langsam, aber sie veralten | Als Referenzdatei mit Datum versionieren. Neu-Scrape von berufsberatung.ch wäre ein eigenes, hier nicht eingeplantes Vorhaben |
| ostjob-iframe-Inserate | Beschreibung unvollständig → schlechteres Embedding | `description_truncated: true`; im Ranking abwerten statt falsch bewerten. iframe wird **nicht** verfolgt |
| Duplikate job-room ↔ ostjob | Doppelte Einträge im Digest | Zweistufiges Dedup mit `apply_url` als starkem Signal. Manuelles Review der ersten ~100 Cluster nötig, um die Schwelle zu kalibrieren |
| Embedding-Kosten / API-Key | Blockiert Tests und Onboarding | Lokaler Fallback (`sentence-transformers`); Embedding-Cache über Text-Hash |
| Score-Kalibrierung ohne Ground Truth | Ranking wirkt beliebig | Deshalb Digest vor Dashboard: erst ~2 Wochen Treffer manuell bewerten, dann Gewichte anhand echter Feedback-Daten anpassen |
| AVAM-Berufscodes nur bei job-room | Klassifikation inkonsistent zwischen Portalen | Codes als optionales Zusatzsignal; die Primärklassifikation kommt aus dem Clustering, das portalunabhängig ist |

### Offene Fragen (vor bzw. während M3 zu klären)

1. **Lohn-Kriterium:** Drei Optionen, Entscheidung sinnvollerweise erst in M5,
   wenn du aus M4 weisst, wie oft Lohn überhaupt im Text steht: (a) **Adzuna-API
   als Benchmark** — einzige geprüfte Quelle mit Lohndaten, gratis, du müsstest
   dich registrieren; (b) BFS-Salarium als statistische Schätzung; (c) Kriterium
   streichen. Empfehlung: (a), weil es echte Inseratslöhne statt Statistik gibt —
   aber erst, wenn der Rest steht.
2. **CV-Format:** Liegt der CV als PDF, Markdown oder LaTeX vor? Bestimmt, ob
   ein Parsing-Schritt nötig ist. Für die Embeddings ist eine gepflegte
   Markdown-Version ohnehin die bessere Grundlage.
3. **Zielprofile:** Ein CV-Vektor oder mehrere (Data Eng / BI / SWE)? Mehrere
   trennen erfahrungsgemäss besser — braucht aber von dir 2–3 kurze
   Profiltexte.
4. **Historie:** Wie lange sollen abgelaufene Inserate aufbewahrt werden? Für
   Marktanalyse wertvoll, für den Digest irrelevant. Vorschlag: unbegrenzt in
   der DB, Digest filtert auf `status = 'active'`.
5. **MongoDB statt SQLite?** Aus dem DSP-Projekt hast du MongoDB-Erfahrung und
   ein fertiges docker-compose. Ich empfehle trotzdem SQLite (Begründung in
   Abschnitt 3) — wenn du MongoDB aus Lern- oder Konsistenzgründen willst, ist
   das eine Konfigentscheidung in M0, kein Umbau.

---

## 6. Nächste Schritte (Milestones)

| # | Milestone | Inhalt | Definition of Done |
|---|---|---|---|
| **M0** | Setup + Referenzdaten | uv-Projekt, `pyproject.toml`, ruff/mypy/pytest, Repo-Skelett, README inkl. «Was dieses Projekt bewusst nicht tut». **Import `berufe_ch.json` aus `~/zhaw/DSP`** (auf gebrauchte Felder reduziert), PLZ-Koordinaten von swisstopo/BFS | `uv run pytest` grün, `ruff check` sauber, 1'851 Berufe + PLZ-Tabelle in SQLite |
| **M1** | Collection job-room | `JobRoomFetcher`, RateLimiter, protego-Check, Rohdaten-Cache, `pipeline fetch job-room --since 1`. Backfill `--since 7` | Tageslauf legt ~663 Roh-JSONs in 2 Requests ab; zweiter Lauf holt dank Cache ~0 neu |
| **M2** | Collection CH-Media + Parsing | **Ein** `ChMediaFetcher` für myjob.ch / ostjob.ch / zentraljob.ch (Host aus Config), Sitemap-Diff → `__PRELOADED_STATE__`; beide Parser, `JobPosting`-Schema, **PII-Allowlist**, Dedup **inkl. MinHash-Near-Dedup je Firma**, `company_is_agency`, Geocoding | SQLite gefüllt; alle drei Hosts laufen über denselben Adapter; PII-Test grün; Dedup auf 100 manuell geprüften Clustern >90% korrekt; MediPersonal-Anteil sinkt von ~40% auf <10% |
| **M3** | Enrichment + **Profile** | Profil-Schema (`profiles/*.yaml`), Zielrollen-Vektoren, Gemini-Embeddings + lokaler Fallback, Embedding-Cache, Positiv-/**Ausschluss**-Regeln, `final_score` mit Breakdown. `--profile`-Flag durchgehend | `pipeline score --profile lucas` und `--profile kollegin` laufen; je Top-20 bei manueller Sichtung plausibel; bei Profil B keine Frontdesk-Stelle in den Top-10 |
| **M4** | **Digest (2 Profile)** | Jinja2-Templates, Top-N gruppiert, Score-Begründung inkl. Gleitzeit-Zitat, Cap pro Arbeitgeber, Neu/Ablaufend-Deltas, lokaler Cron | Beide bekommen 14 Tage lang täglich einen Digest und bewerten ihn — das Feedback beider Profile kalibriert M6 |
| **M5** | Clustering + Gap-Analyse | UMAP → HDBSCAN; **Cluster-Labeling gegen die 65 Informatik-Berufe** aus `berufe_ch.json`; Skill-Dictionary (Berufs-Vokabular + Tech-Liste); Gap-Report mit `career_progression` | `02_clusters.ipynb` zeigt eine interpretierbare Karte der WI/DS-Jobtypen mit offiziellen Berufsbezeichnungen |
| **M6** | **Dashboard** | Streamlit: Filter, Sortierung, Cluster-Scatter, Skill-Gaps, verwandte Berufe, Feedback-Erfassung; Gewichte anhand der M4-Bewertungen kalibriert | Dashboard läuft lokal; Gewichte sind datenbasiert statt geraten |
| **M7** | Anschreiben-Agent (opt-in) | Entwurf pro Top-Treffer aus CV + Inserat; Prompt-/PDF-Bausteine aus `swiss-cv-generator` übernommen; Ausgabe nach `drafts/`, kein Versand | Ein Entwurf ist gut genug, dass du ihn überarbeitest statt neu schreibst |

**Kritischer Pfad: M0 → M1 → M2 → M3 → M4.** M5 ist der Portfolio-Höhepunkt,
aber fachlich unabhängig — kann parallel oder später laufen. M7 ist optional und
für das Ziel «passende Stellen finden» nicht erforderlich.

**Empfohlener Einstieg:** M0 + M1 zusammen. Der `onlineSince=7`-Backfill gibt dir
sofort ~6'000 Inserate im Rohcache; nach zwei weiteren Wochen täglicher Läufe
sind es ~15'000. M2/M3 entwickelst du dann gegen echte, lokale Daten — ohne die
Portale bei jedem Testlauf erneut anzufassen.

---

## Verifikation

Alles unten wurde am 27.07.2026 gegen die Live-Portale geprüft und ist so
reproduzierbar:

```bash
# job-room Such-API: öffentlich, ohne Auth — Tagesinkrement ZH/SG/TG
curl -s -D - -o /dev/null \
  -H "Content-Type: application/json" \
  -X POST "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search?page=0&size=1&sort=date_desc" \
  -d '{"onlineSince":1,"displayRestricted":false,"keywords":[],"cantonCodes":["ZH","SG","TG"],"professionCodes":[],"communalCodes":[],"workloadPercentageMin":0,"workloadPercentageMax":100}' \
  | grep -i x-total-count
```

```bash
# offizielle Publikations-API: 401, wie erwartet
curl -s -o /dev/null -w "%{http_code}\n" "https://api.job-room.ch/api/"
```

```bash
# ostjob: 410 Gone bei abgelaufenen Inseraten, 200 bei aktiven
curl -s -o /dev/null -w "%{http_code}\n" "https://www.ostjob.ch/job/x/900000"
```

**Laufende Verifikation im Projekt:**

- `pytest tests/test_pii.py` — kein Kontaktfeld überlebt den Parser
- `pytest tests/test_contracts.py` — Fetcher-Antwort passt zum erwarteten Schema
  (gegen eingecheckte Fixtures, offline lauffähig)
- `pipeline fetch --dry-run` — zeigt geplante Requests inkl. robots.txt-Auswertung
  und Rate-Limit, ohne einen Request zu senden
- `pipeline doctor` — prüft robots.txt aller Quellen gegen den aktuellen UA und
  bricht ab, wenn eine Quelle uns inzwischen ausschliesst

---

## Quellen

- [jobs.ch Nutzungsbedingungen](https://www.jobs.ch/de/nutzungsbedingungen/) ·
  [robots.txt](https://www.jobs.ch/robots.txt) ·
  [Job-Sitemap](https://www.jobs.ch/sitemaps/jobs/de/sitemap.xml)
- [ostjob.ch robots.txt](https://www.ostjob.ch/robots.txt) ·
  [Sitemap](https://www.ostjob.ch/sitemap.xml) ·
  [Nutzungsbedingungen](https://www.ostjob.ch/nutzungsbedingungen) ·
  [AGB](https://www.ostjob.ch/AGB)
- [job-room.ch robots.txt](https://www.job-room.ch/robots.txt) ·
  [Job-Room Jobs API v1.0 (SECO)](https://test-api.job-room.ch/api-docs/jobAdvertisements/v1/index.html) ·
  [arbeit.swiss Stellenmeldepflicht](https://www.arbeit.swiss/secoalv/de/home/menue/unternehmen/stellenmeldepflicht.html)
- Apify-Referenzen für Feldnamen:
  [ostjob](https://apify.com/santamaria-automations/ostjob-ch-scraper) ·
  [arbeit.swiss](https://apify.com/santamaria-automations/arbeit-swiss-scraper)
- [BFS Salarium-Lohnrechner](https://www.gate.bfs.admin.ch/salarium/) (optionaler Lohn-Benchmark)
- Geprüfte, aber verworfene Quellen:
  [Adzuna API-Doku](https://developer.adzuna.com/overview) ·
  [Adzuna ToS](https://developer.adzuna.com/docs/terms_of_service) ·
  [Careerjet Partner-API](https://www.careerjet.ch/partners/api/) ·
  [jobagent.ch robots.txt](https://www.jobagent.ch/robots.txt) ·
  [myjob.ch robots.txt](https://www.myjob.ch/robots.txt)
