# Tankagent

Preistracker für eine einzelne Tankstelle: JET, Wormser Str. 135, Mainz.

Ein GitHub-Actions-Workflow liest von 6 bis 24 Uhr alle 15 Minuten die Preise von jet.de.
Ändert sich ein Preis oder der Preisstand, wird eine Zeile an `data/prices.csv` angehängt und committet.
Optional kommt eine Push-Nachricht über ntfy. Die Seite `index.html` zeigt den Dieselverlauf über GitHub Pages.

## Dateien

| Datei | Zweck |
|---|---|
| `collector.py` | Abruf, Parser, Speicherung und Push, nur Standardbibliothek |
| `test_collector.py` | Tests ohne Netzwerkzugriff |
| `index.html` | Seite mit aktuellem Preis und Verlaufsdiagramm |
| `.nojekyll` | GitHub Pages liefert die Dateien unverändert aus |
| `.github/workflows/collect.yml` | geplanter Lauf |
| `data/prices.csv` | Messdaten |

## Einstellungen im Repository

Unter Settings, Secrets and variables, Actions:

| Name | Art | Beispiel | Wirkung |
|---|---|---|---|
| `NTFY_TOPIC` | Secret | `tankagent-3f9c1a7e5b2d4c60` | ntfy-Thema; ohne dieses Secret wird nichts gesendet |
| `DIESEL_ALERT_BELOW` | Variable | `1.999` | Nachricht, sobald Diesel unter diesen Wert fällt |
| `NOTIFY_DIESEL_CHANGE` | Variable | `true` | Nachricht bei jeder Änderung des Dieselpreises |

Die Ruhezeit steht im Workflow als `QUIET_HOURS: "0-6"` (deutsche Ortszeit).

## Datenquelle

1. `https://www.jet.de/api/v2/station/price-table/update/fuel?station=31e1ff9c20`
   Diesen Endpunkt ruft die Stationsseite selbst alle 60 Sekunden ab. Er liefert JSON mit einem HTML-Fragment der Preistabelle.
   Er ist nicht dokumentiert und kann sich ohne Ankündigung ändern.
2. Fallback: die Stationsseite `https://www.jet.de/tankstellen/mainz/wormser-str-135`.

## Datenformat

~~~
fetched_utc,price_stand,diesel,super,super_e10,super_plus,source
2026-11-16T21:57:40Z,2026-11-16T20:13:00+01:00,2.119,1.879,1.819,1.989,api
~~~

- `fetched_utc`: Zeitpunkt der Abfrage in UTC
- `price_stand`: Angabe "Stand der Preise" von jet.de, als deutsche Ortszeit interpretiert
- Preise in Euro pro Liter, leer wenn die Seite keinen Preis zeigt
- `source`: `api` oder `page`
- Eine Zeile gilt, bis die nächste Zeile folgt

## Grenzen der Messung

- Ändert JET die Seitenstruktur, bricht der Parser. Der Workflow schlägt dann fehl, und GitHub meldet das per E-Mail.
- Preisphasen, die kürzer als das Abfrageintervall sind, können fehlen. Zwischen 0 und 6 Uhr wird nicht gemessen.
- Fällt der Workflow aus, entsteht eine Lücke, die in der CSV wie ein konstanter Preis aussieht.
- Das Diagramm setzt den Zeitpunkt einer Änderung auf `price_stand`, sonst auf `fetched_utc`.

## Lokal ausführen

~~~
python3 -m unittest -v
python3 collector.py
~~~
