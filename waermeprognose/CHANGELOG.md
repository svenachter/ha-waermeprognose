# Changelog

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
