# Wärmeprognose

Prognose von Wärmebedarf (Heizung inkl. Pool + Warmwasser), Vorlauftemperatur, COP und
Stromaufnahme der Wärmepumpe im 15-Minuten-Raster für 48–72 h. Läuft alle 15 Minuten,
kalibriert sich nachts selbst und stellt das Ergebnis per REST-API bereit.

## Rechenkette

| Schritt | Quelle / Formel |
|---|---|
| Außentemperatur | open-meteo `minutely_15`, korrigiert um die aktuelle Abweichung des Lambda-Außenfühlers (klingt mit 6 h ab) |
| Gedämpfte Temperatur | exponentielle Glättung, `damping_hours` (12 h) |
| Heizlast | `q = q_base + slope·Td + knee_slope·max(0, knee − Td) − solar·GTI` |
| Warmwasser | Viertelstundenprofil aus Lambda-Trendlogs (`/share/waermeprognose/`) und/oder HA-Statistik |
| Vorlauf | höchster Heizkurven-Sollwert der Heizkreise (live aus HA) + `flow_offset_hp`; Warmwasser `flow_dhw` |
| COP | `η · (T_VL + 273,15) / (T_VL − T_Quelle)`, `T_Quelle = T_außen + source_temp_offset`; η = `eta_frost` bei T_Quelle ≤ `frost_threshold` |
| Strom | `Q / COP · power_correction`; Wärme oberhalb der Maximalleistung (Datenblatt) → Heizstab mit COP 1 |

### Startwerte (aus Messdaten 11.–16.11.2026)

- η = 0,56 / 0,51 bei Frost – Stunden-COP inkl. Abtauung, Stromwert per Hausverbrauch geprüft
- `power_correction` = 1,07 – Regler-Stromwert lag 5–10 % unter dem Hausverbrauch
- `flow_offset_hp` = −3 K – Wärmepumpen-Vorlauf lag im Mittel 3 K unter dem Heizkurven-Soll
- Heizlast 7,2 kW nahezu temperaturunabhängig (−0,05 kW/K): Im November lief die Wärmepumpe
  an der Mindestmodulation, eine Temperaturabhängigkeit war noch nicht messbar
- Knick bei −3 °C mit 1,76 kW/K: **Annahme**, damit bei −10 °C die Auslegungslast von ~20 kW
  erreicht wird. Wird erst kalibriert, wenn Messdaten unterhalb des Knicks vorliegen.

## Nächtliche Kalibrierung

Zur eingestellten Stunde (Standard 3 Uhr) werden aus den Langzeitstatistiken der letzten
`history_days` Tage neu geschätzt:

- Heizlast-Parameter per Ridge-Regression auf 6-h-Blöcken; die Startwerte wirken wie
  `prior_hours` Stunden zusätzlicher Daten. Knick und Solargewinn werden nur angepasst,
  wenn die Daten sie hergeben (Temperaturen unter dem Knick, schwankende Einstrahlung).
- `flow_offset_hp` als Median-Abstand Ist-Vorlauf − Heizkurve in reinen Heizstunden.
- Prognose-Unsicherheit (p10/p90) aus dem Kalibrierfehler.

η wird bewusst **nicht** automatisch kalibriert, weil der Stromwert der Wärmepumpe aus dem Regler
stammt und nicht aus einem eigenen Zähler.

## Warmwasser-Trendlogs

Exportierte Lambda-Trendlogs (xlsx oder csv, beliebig viele) nach
`/share/waermeprognose/` legen. Benötigt werden die Spalten
`[key:"boiler.3"]` (Ladepumpe / 3-Wege-Ventil) und `[key:"heatpump.17"]` (thermische Leistung);
Abtauminuten (`[key:"heatpump.21"]` = 10) werden ignoriert. Nur vollständige Tage zählen.
Mit `source: both` ergänzt die HA-Statistik alle Tage, die nicht im Logfile stehen.
Ab je 3 Werktagen und Wochenendtagen werden getrennte Profile geführt.

## REST-API (Port 8099)

Authentifizierung: `Authorization: Bearer <api_token>` oder `X-API-Key: <api_token>`.
Ohne gesetztes Token ist die API offen – nur im vertrauenswürdigen Heimnetz verwenden.

| Methode | Pfad | Inhalt |
|---|---|---|
| GET | `/api/v1/health` | Status, letzter Lauf, Fehler (ohne Token) |
| GET | `/api/v1/forecast?horizon=48&resolution=15min\|60min` | Zeitreihe + Metadaten |
| GET | `/api/v1/forecast/summary` | Tagessummen (kWh), nächste Stunde |
| GET | `/api/v1/params` | aktive Parameter, Kalibrierung, Warmwasserprofil |
| GET | `/api/v1/backtest?days=7&lead_hours=24` | Prognose (mit Vorlauf) gegen Ist je Tag |
| POST | `/api/v1/refresh` | Prognose sofort neu rechnen |
| POST | `/api/v1/calibrate` | Kalibrierung sofort ausführen |

Interaktive Doku: `http://<HA-IP>:8099/docs`.

Felder je Slot (`series[]`): `t` (Slot-Beginn, lokal), `t_out`, `t_damped`, `gti`,
`q_heat_kw`, `q_dhw_kw`, `q_total_kw`, `flow_temp`, `cop_heat`, `cop_dhw`, `cop_system`,
`p_el_kw`, `p_backup_kw`, `frost_risk`, `below_min_modulation`, `q_heat_p10/p90`, `p_el_p10/p90`.
Leistungen sind Mittelwerte über den Slot (kW); Energie = kW × 0,25 h.

## Sensoren in Home Assistant

`sensor.waermeprognose_waerme_heute_rest`, `…_morgen`, `…_uebermorgen`,
`sensor.waermeprognose_strom_heute_rest`, `…_morgen`, `…_uebermorgen` (kWh),
`sensor.waermeprognose_waerme_naechste_stunde`, `sensor.waermeprognose_strom_naechste_stunde` (kW),
`sensor.waermeprognose_status`.
Die Sensoren werden über die REST-API gesetzt (keine Registry-Einträge) und sind nach einem
HA-Neustart bis zum nächsten Lauf (≤ 15 min) leer.

## Grenzen

- Heizlast ist nach 6 Tagen Betrieb kaum identifizierbar; die Kalibrierung wird mit jeder
  Woche besser, verlässliche Werte für Frost erst nach echten Frosttagen.
- Pool, Lüftung und Hauskreise sind gemeinsam modelliert (wie gewünscht).
- Takten unterhalb der Mindestmodulation (`below_min_modulation`) wird nur markiert,
  nicht mit einem COP-Abschlag versehen.
