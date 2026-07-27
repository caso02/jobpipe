# Suchprofile

Ein Profil beschreibt **eine suchende Person**: Lebenslauf, Zielrollen,
Wunschort, Muss- und Ausschlusskriterien, Gewichte.

## Wichtig: nichts hier wird eingecheckt

Bis auf `example.yaml` und diese README sind alle Dateien in diesem Ordner per
`.gitignore` ausgeschlossen. Profile enthalten Personendaten — CV-Texte,
Arbeitgeber, teilweise auch Angaben zu Dritten. Die gehören nicht in ein
öffentliches Portfolio-Repo.

```bash
cp profiles/example.yaml profiles/meinname.yaml
$EDITOR profiles/meinname.yaml
```

## Der wichtigste Teil: `target_roles`

Naheliegend wäre, einfach den CV einzubetten und nach Ähnlichem zu suchen. Das
funktioniert nur, wenn man **mehr vom Gleichen** will.

Wer sich wegbewerben möchte, bekommt so das Gegenteil des Gewünschten: Der
Lebenslauf beschreibt den Ist-Zustand, und die ähnlichsten Inserate sind genau
die Stellen, die man gerade verlassen will.

Deshalb sind `target_roles` getrennt vom `cv_text` und beschreiben den
**Wunschzustand**. Der semantische Score nimmt das Maximum über alle
Zielrollen — zwei bis vier Stossrichtungen trennen deutlich besser als ein
einzelner gemittelter Text.

## `keywords_exclude`

Weiche Negativ-Signale, kein harter Filter. Ein Inserat mit einem Treffer kann
trotzdem oben landen, wenn alles andere passt. Das ist Absicht: harte Filter
auf Textmuster werfen zu viel weg.
