# Changelog

## 0.4.0 – 2026-11-25

- Neue Plan/Ist-Tabelle als HTML im Stil der Predbat-Plan-Karte: Sensor
  `sensor.waermeprognose_plan_html` (Attribut `html`) für die HTML-Jinja2-Template-Karte und
  Seite `GET /api/v1/plan.html`. Je Tag aufklappbar: Heute und Morgen offen, die letzten
  `plan_view.days` Tage (Standard 7) im Rückblick. Spalten Wärme, davon WW, WP-Strom, COP, PV,
  Haushalt, Netz, BHKW elektrisch/thermisch, Puffertemperaturen, Kosten und CO₂.
- Vergangene Stunden zeigen Prognose (Vorlauf `compare.lead_hours`) gegen Ist, eingefärbt nach
  der Abweichung (≤ 10 % grün, ≤ 25 % gelb, > 25 % rot); ab der Linie die aktuelle Prognose.
- Kosten: Ist = Netzbezug × Strompreis − Einspeisung × Vergütung + BHKW-Gas × Gaspreis;
  Prognose = reiner Netzbedarf (ohne Batterie und BHKW). CO₂: Netzbezug × stündliche
  Netzintensität (Ist aus der Statistik, Prognose aus der Prognosereihe) + Gas × Faktor.
- Neue Optionsgruppe `plan_view` (Entitäten, Einspeisevergütung, BHKW-Wärmeverhältnis,
  CO₂-Faktor Gas). Bestehende Konfigurationen übernehmen die Standardwerte automatisch.

## 0.3.0 – 2026-11-18

- Abgleich Prognose ↔ Ist für die letzten 48 h, Stunde für Stunde: Wärme (Heizung + WW),
  WP-Strom, PV und Haushalt mit prozentualer Abweichung. Es wird die gespeicherte Prognose
  genutzt, deren Vorlauf `compare.lead_hours` (Standard 24 h) am nächsten kommt, mindestens
  `compare.min_lead_hours` (1 h).
- Neuer Endpunkt `GET /api/v1/compare?hours=48&lead_hours=24` und Sensor
  `sensor.waermeprognose_abgleich` (Zustand = Wärme-Abweichung der 48 h in %, Attribute
  `summary` und `rows`), stündlich aktualisiert.
- Gespeicherte Stundenprognosen enthalten jetzt auch PV und Haushalt (Datenbank wird beim Start
  automatisch erweitert; ältere Einträge haben dort keine Werte).

## 0.2.1 – 2026-11-18

- Neuer Sensor `sensor.waermeprognose_zeitreihe`: Attribut `forecast` mit 48 Stundenwerten
  (t, ta, vl, q, qh, qw, cop, wp, hh, pv, net, frost) für Tabellen und Diagramme im Dashboard.

## 0.2.0 – 2026-11-17

- Heizkreise HC3 (Pool, fester Sollwert) und HC4 (Lüftung) aufgenommen; neuer Modus je Kreis
  (`curve`/`target`) und Auswertung des Betriebsstatus (inaktive Kreise werden ignoriert).
- PV-Prognose aus Helios Forecast (`pv_kw`, `pv_p10`, `pv_p90`).
- Haushaltslastprofil aus den letzten 3 Werktagen bzw. Wochenendtagen (`household_kw`),
  Mittelwert oder Median, um den Wärmepumpen-Anteil korrigiert.
- Netto-Strombedarf `net_kw` und Tagessummen `pv_kwh`, `household_kwh`, `net_import_kwh`,
  `surplus_kwh`; neue HA-Sensoren für Haushalt, PV und Netzbedarf.
- **Hinweis Update:** Eine bereits gespeicherte Konfiguration enthält nur HC1/HC2. Für HC3/HC4
  die Konfiguration auf Standard zurücksetzen (Token danach neu eintragen) oder die Kreise im
  YAML-Modus ergänzen.

## 0.1.2 – 2026-11-17

- Build robuster bei langsamer Verbindung: alle 22 Pakete fest gepinnt (kein Backtracking),
  nur Binär-Wheels, pip-Timeout 120 s mit 10 Wiederholungen, Installation bis zu 3× wiederholt.

## 0.1.1 – 2026-11-17

- Build-Fehler behoben: Python-Basis-Image fest im Dockerfile, `build.yaml` entfernt
  (der Supervisor nutzte sonst `ghcr.io/home-assistant/base` ohne Python/pip).

## 0.1.0 – 2026-11-17

- Erste Version: open-meteo 15-min-Prognose, Heizlast- und COP-Modell, Warmwasserprofil aus
  Lambda-Trendlogs und HA-Statistik, REST-API, HA-Sensoren, nächtliche Kalibrierung, Backtest.
