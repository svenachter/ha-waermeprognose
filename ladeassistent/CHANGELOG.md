# Changelog

## 0.1.3
- Meldungen mit hoher Priorität (Android `priority: high`, `ttl: 0`, eigener Kanal „Lade-Assistent“; iOS time-sensitive), damit sie auch im Energiesparmodus sofort ankommen.

## 0.1.2
- Benachrichtigung ohne `return_response` aufrufen (notify-Dienste liefern keine Antwort, HA meldete 400).

## 0.1.1
- Verbrauch pro km über ganze Fahrstrecken zwischen zwei Ladungen statt über Sample-Paare
  (Auto-Clouds melden den Ladestand oft später als den Kilometerstand).

## 0.1.0
- Erste Version: lernt Tagesverbrauch je Wochentag, Verbrauch pro km und Standzeiten zu Hause,
  plant 48 h voraus gegen PV-Überschussprognose und Strompreis, meldet sich per Mobile App
  (Aktionen „OK“ / „Heute nicht“). Nur Empfehlungen, keine Steuerbefehle an die Wallbox.
