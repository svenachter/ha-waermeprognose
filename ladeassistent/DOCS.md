# Lade-Assistent

Lernt aus dem Verlauf des Autos (Ladestand, Kilometerstand, Standort, Stecker), wie viel Energie
an welchem Wochentag üblicherweise verbraucht wird und wann das Auto zu Hause steht. Daraus:

- **Reichweite in Tagen** bei üblicher Nutzung (vorsichtiges Quantil, Standard 80 %),
- **günstigstes Ladefenster** in den nächsten 48 h: PV-Überschuss wird mit der Einspeisevergütung
  bewertet, Netzstrom mit dem aktuellen Preis; Überschuss zählt erst ab der Mindestladeleistung,
- **Benachrichtigung** aufs Handy, wenn sich Anstecken lohnt (Ersparnis ≥ Mindestbetrag, Fenster
  innerhalb des Vorlaufs) oder wenn die Reichweite knapp wird. Ruhezeit, Tageslimit und
  „Heute nicht“ (pausiert für 24 h) verhindern zu viele Meldungen.

Keine Eingabe von Abfahrtszeiten nötig. Die App schreibt nichts an Wallbox oder Auto.

## Sensoren
- `sensor.ladeassistent_status`: `opportunity`, `urgent`, `plugged`, `ok`, `unknown`; Attribute mit
  Text, Plan je Stunde, gelerntem Verbrauch und Wochentagsprofil.
- `sensor.ladeassistent_reichweite_tage`
- `sensor.ladeassistent_naechstes_fenster` (Zeitstempel, Attribute `end`, `kwh_surplus`)

## Testmeldung
Ereignis `ladeassistent_test` auslösen (Entwicklerwerkzeuge → Ereignisse): die aktuelle Empfehlung
wird sofort verschickt.

## Andere Profile
Alle Entitäten sind Optionen. Ohne Standort-Sensor zählt der Wallbox-Stecker als „zu Hause“; ohne
Überschussprognose bleibt die Reichweiten-Warnung. Pendler-Muster (Wochentage) lernt das Modell aus
dem Kilometerstand; nach etwa 3 Wochen gibt es eigene Werte je Wochentag, vorher gepoolt.
