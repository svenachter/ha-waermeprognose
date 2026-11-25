# ha-waermeprognose

Home-Assistant-App-Repository mit der App **Wärmeprognose** für die Lambda EU20L:
Wärme-, COP- und Strombedarfsprognose im 15-Minuten-Raster, abrufbar per REST-API für einen
nachgelagerten Optimierer. Details: [waermeprognose/DOCS.md](waermeprognose/DOCS.md).

Außerdem die App **Lade-Assistent**: lernt Fahrverhalten und Standzeiten des E-Autos und meldet
günstige PV-Ladefenster per Mobile App. Details: [ladeassistent/DOCS.md](ladeassistent/DOCS.md).

## Installation

1. *Einstellungen → Apps → App-Store* → ⋮ → *Repositories*
2. `https://github.com/svenachter/ha-waermeprognose` hinzufügen.
3. „Wärmeprognose“ installieren (die App wird auf dem Host gebaut, das dauert einige Minuten).

[![Repository zu Home Assistant hinzufügen](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsvenachter%2Fha-waermeprognose)

Updates: Version in `waermeprognose/config.yaml` erhöhen und pushen – Home Assistant bietet
das Update dann im App-Store an.

## Einrichtung

1. `api_token` setzen (empfohlen).
2. Fassaden für Solargewinne eintragen (open-meteo-Azimut: 0 = Süd, −90 = Ost, 90 = West).
3. Lambda-Trendlogs nach `/share/waermeprognose/` legen (optional, für das Warmwasserprofil).
4. App starten, Log prüfen, `http://<HA-IP>:8099/docs` öffnen.

## Abruf durch den Optimierer

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://homeassistant.local:8099/api/v1/forecast?horizon=48&resolution=15min"
```

## Entwicklung

```bash
cd waermeprognose
pip install -r requirements.txt pytest
python -m pytest -q
# Rechenkette offline gegen Trendlog + Statistik-Export prüfen:
python ../tools/validate_offline.py TRENDLOG.xlsx STATISTICS.xlsx
```
