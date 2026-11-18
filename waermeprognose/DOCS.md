# Wärmeprognose

Prognose von Wärmebedarf (Heizung inkl. Pool + Warmwasser), Vorlauftemperatur, COP und
Stromaufnahme der Wärmepumpe – zusammen mit PV-Ertrag (Helios Forecast), Haushaltslast und
Netto-Strombedarf – im 15-Minuten-Raster für 48–72 h. Läuft alle 15 Minuten,
kalibriert sich nachts selbst und stellt das Ergebnis per REST-API bereit.

## Rechenkette

| Schritt | Quelle / Formel |
|---|---|
| Außentemperatur | open-meteo `minutely_15`, korrigiert um die aktuelle Abweichung des Lambda-Außenfühlers (klingt mit 6 h ab) |
| Gedämpfte Temperatur | exponentielle Glättung, `damping_hours` (12 h) |
| Heizlast | `q = q_base + slope·Td + knee_slope·max(0, knee − Td) − solar·GTI` |
| Warmwasser | Viertelstundenprofil aus Lambda-Trendlogs (`/share/waermeprognose/`) und/oder HA-Statistik |
| Vorlauf | höchster Sollwert aller **aktiven** Heizkreise (siehe unten) + `flow_offset_hp`; Warmwasser `flow_dhw` |
| COP | `η · (T_VL + 273,15) / (T_VL − T_Quelle)`, `T_Quelle = T_außen + source_temp_offset`; η = `eta_frost` bei T_Quelle ≤ `frost_threshold` |
| Strom | `Q / COP · power_correction`; Wärme oberhalb der Maximalleistung (Datenblatt) → Heizstab mit COP 1 |
| PV | Aktion `helios_forecast.get_forecast` (15-min, W → kW, P10/P90) |
| Haushalt | Mittel (oder Median) der letzten 3 vollständigen Werktage bzw. Wochenendtage je Viertelstunde |
| Netto | `net_kw = p_el_kw + household_kw − pv_kw` (> 0 Bedarf aus Netz/Batterie/BHKW, < 0 Überschuss) |

### Heizkreise

Jeder Heizkreis hat einen Modus:

- `curve`: Sollwert aus den Heizkurven-Stützpunkten (−22/0/+22 °C) + Offset, gerechnet mit der
  prognostizierten gedämpften Außentemperatur (Standard für HC1, HC2, HC4).
- `target`: der aktuelle Sollwert aus `target_flow` wird über den Horizont gehalten
  (Standard für HC3/Pool, der fest mit 30 °C fährt statt nach Heizkurve).

Kreise, deren Betriebsstatus (`state`) in `inactive_states` steht (z. B. `RELEASE-OFF`,
`CH-SUMMER`, `OFF`), oder ohne Sollwert > 0 werden ignoriert. Der Status wird bei jedem Lauf neu
gelesen und gilt für den ganzen Horizont. Die API zeigt unter `circuits` je Kreis, ob und mit
welchem Sollwert er berücksichtigt wurde.

Die **Wärmemenge** aller vier Kreise steckt bereits im Heizwärmezähler der Wärmepumpe und damit
in der kalibrierten Heizlast; getrennte Wärmemengen je Kreis gibt es mangels eigener Zähler nicht.

### Haushaltslast

Quelle ist `entities.household_power` (Standard `sensor.hausverbrauch_ohne_warmepumpe`, also
ohne Wärmepumpe und ohne Wallbox). Mit `correct_hp_share` wird zusätzlich
`(power_correction − 1) × entities.hp_power` abgezogen, weil der Regler-Stromwert der
Wärmepumpe rund 7 % zu niedrig ist. Daten: 5-min-Statistik (≈ 10 Tage), ältere Tage aus der
Stundenstatistik. Das Profil wird täglich neu gebildet.

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
| GET | `/api/v1/compare?hours=48&lead_hours=24` | Stündlich: gespeicherte Prognose gegen Ist mit Abweichung in % |
| POST | `/api/v1/refresh` | Prognose sofort neu rechnen |
| POST | `/api/v1/calibrate` | Kalibrierung sofort ausführen |

Interaktive Doku: `http://<HA-IP>:8099/docs`.

Felder je Slot (`series[]`): `t` (Slot-Beginn, lokal), `t_out`, `t_damped`, `gti`,
`q_heat_kw`, `q_dhw_kw`, `q_total_kw`, `flow_temp`, `cop_heat`, `cop_dhw`, `cop_system`,
`p_el_kw`, `p_backup_kw`, `frost_risk`, `below_min_modulation`, `q_heat_p10/p90`, `p_el_p10/p90`,
`pv_kw`, `pv_p10/p90` (fehlende Bänder = Mittelwert), `household_kw`, `net_kw`.
Tagessummen zusätzlich: `pv_kwh`, `household_kwh`, `net_import_kwh`, `surplus_kwh`.
Leistungen sind Mittelwerte über den Slot (kW); Energie = kW × 0,25 h.

## Sensoren in Home Assistant

`sensor.waermeprognose_waerme_heute_rest`, `…_morgen`, `…_uebermorgen`,
`sensor.waermeprognose_strom_heute_rest`, `…_morgen`, `…_uebermorgen` (kWh),
`sensor.waermeprognose_haushalt_*`, `sensor.waermeprognose_pv_*`,
`sensor.waermeprognose_netzbedarf_*` (gleiche Tage, kWh),
`sensor.waermeprognose_waerme_naechste_stunde`, `sensor.waermeprognose_strom_naechste_stunde` (kW),
`sensor.waermeprognose_status`,
`sensor.waermeprognose_zeitreihe` (Zustand = Zeitpunkt der Prognose; Attribut `forecast` mit
48 Stundenwerten: `t` Stunde, `ta` Außen °C, `vl` Vorlauf °C, `q`/`qh`/`qw` Wärme gesamt/Heizung/WW kW,
`cop`, `wp` Strom WP kW, `hh` Haushalt kW, `pv` PV kW, `net` Netto kW, `frost`).
Die Sensoren werden über die REST-API gesetzt (keine Registry-Einträge) und sind nach einem
HA-Neustart bis zum nächsten Lauf (≤ 15 min) leer.

## Abgleich Prognose ↔ Ist

Für jede der letzten `compare.hours` Stunden wird aus der Datenbank die Prognose gewählt, deren
Vorlauf am nächsten an `compare.lead_hours` liegt (Standard 24 h = „wie am Vortag vorhergesagt“),
aber mindestens `compare.min_lead_hours` vor Stundenbeginn erstellt wurde. Verglichen werden:

| Größe | Ist-Wert |
|---|---|
| Wärme | Heizwärme- + WW-Wärmezähler (Änderung je Stunde) |
| WP-Strom | Heiz- + WW-Stromzähler × `power_correction` (gleiche Basis wie die Prognose) |
| PV | Stundenmittel von `entities.pv_power` |
| Haushalt | Stundenmittel von `entities.household_power` (mit WP-Korrektur wie im Profil) |

Abweichung = Prognose / Ist − 1. Bei sehr kleinen Ist-Werten (Wärme < 0,2 kWh, sonst
< 0,05 kWh, z. B. PV nachts) wird keine Prozentzahl ausgewiesen. Die Zusammenfassung enthält
die Summen-Abweichung und die mittlere absolute Stundenabweichung. Sensor:
`sensor.waermeprognose_abgleich` (stündlich aktualisiert). Nach der Installation dauert es
`lead_hours` Stunden, bis die ersten Vergleiche mit diesem Vorlauf möglich sind.

## Grenzen

- Heizlast ist nach 6 Tagen Betrieb kaum identifizierbar; die Kalibrierung wird mit jeder
  Woche besser, verlässliche Werte für Frost erst nach echten Frosttagen.
- Pool, Lüftung und Hauskreise sind gemeinsam modelliert (wie gewünscht).
- Der Netto-Bedarf berücksichtigt weder Batterie noch BHKW noch Wallbox – das plant der Optimierer.
- Takten unterhalb der Mindestmodulation (`below_min_modulation`) wird nur markiert,
  nicht mit einem COP-Abschlag versehen.
