# Changelog

## 0.1.2 – 2026-11-17

- Build robuster bei langsamer Verbindung: alle 22 Pakete fest gepinnt (kein Backtracking),
  nur Binär-Wheels, pip-Timeout 120 s mit 10 Wiederholungen, Installation bis zu 3× wiederholt.

## 0.1.1 – 2026-11-17

- Build-Fehler behoben: Python-Basis-Image fest im Dockerfile, `build.yaml` entfernt
  (der Supervisor nutzte sonst `ghcr.io/home-assistant/base` ohne Python/pip).

## 0.1.0 – 2026-11-17

- Erste Version: open-meteo 15-min-Prognose, Heizlast- und COP-Modell, Warmwasserprofil aus
  Lambda-Trendlogs und HA-Statistik, REST-API, HA-Sensoren, nächtliche Kalibrierung, Backtest.
