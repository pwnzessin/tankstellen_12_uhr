# Tankstellen: 12-Uhr-Regel Analyse

Prueft, welche Tankstellen in einem Umkreis gegen die 12-Uhr-Regel (§ 2 KPAnG, seit 1.4.2026) verstossen:
Preise duerfen nur einmal pro Kalendertag um 12:00 Uhr **erhoeht** werden, Senkungen sind jederzeit erlaubt.
Daten: Tankerkoenig API (MTS-K), gespeichert in SQLite.

## Setup

```powershell
pip install -r requirements.txt          # nur tzdata (Windows hat keine Zeitzonen-DB)
copy config.example.json config.json     # API-Key + Koordinaten eintragen
```

API-Key: https://creativecommons.tankerkoenig.de (Demo-Key liefert nur Fake-Preise).

## Benutzung

```powershell
# einmal abfragen (Radius max. 25 km)
python -m tankstellen poll --lat 52.1833 --lng 8.6833 --radius 10

# dauerhaft sammeln (alle ~5 min mit Zufallsversatz; Strg+C beendet)
python -m tankstellen daemon --lat 52.1833 --lng 8.6833 --radius 10

# Auswertung
python -m tankstellen report
python -m tankstellen report --since 2026-10-08
```

Alle Werte koennen auch in `config.json` stehen; der Key auch in der Umgebungsvariable `TANKERKOENIG_API_KEY`.
`--db` waehlt die SQLite-Datei (Standard `tankstellen.db`).

## Historische Daten importieren (exakte Auswertung)

Mit einem freigeschalteten Tankerkoenig-Account gibt es die Preisaenderungen seit 2014 als CSV-Dateien
(Git-Repo, siehe https://onboarding.tankerkoenig.de). Dort steht der **exakte Zeitpunkt jeder Aenderung**,
dadurch ist die Auswertung eindeutig statt fensterbasiert.

```powershell
# Repo klonen/entpacken, dann den Ordner (oder eine Datei / ein .zip) importieren.
# Nur Tankstellen im Umkreis werden gespeichert (hier gilt kein 25-km-Limit).
python -m tankstellen import C:\daten\tankerkoenig-data --lat 52.1833 --lng 8.6833 --radius 25

python -m tankstellen report --since 2026-04-01
python -m tankstellen report --tolerance 120      # Toleranz nach 12:00:00 in Sekunden (Standard 300)
```

- Erwartete Formate: `*-prices.csv` mit `date,station_uuid,diesel,e5,e10,dieselchange,e5change,e10change`
  und `*-stations.csv` mit `uuid,name,brand,street,house_number,post_code,city,latitude,longitude`.
  Spaltenreihenfolge, BOM, Dezimalkomma und Preise in Zehntel-Cent werden toleriert.
- Reihenfolge: erst alle Stations-Dateien, dann die Preis-Dateien chronologisch (nach Dateiname/Datum).
  Stationen aus frueheren Imports oder aus `poll` zaehlen ebenfalls als bekannt.
- Bereits importierte Dateien (nach Dateiname) werden uebersprungen: ein erneuter Import ist gefahrlos,
  neue Tage koennen nachgeladen werden.
- Nur Aenderungs-Flag `1` zaehlt als Preisaenderung. `3` (neu / nach MTS-K-Ausfall) setzt die Preisreihe
  neu auf, `2` (entfernt) loescht den letzten Preis; beides ist nie eine "Erhoehung".

**Bewertung jeder Erhoehung (einzeln):**

| Verstoss | Bedeutung |
|---|---|
| `outside` | Erhoehung liegt ausserhalb von `[12:00:00, 12:00:00 + Toleranz)` (Berliner Zeit, auch bei Zeitumstellung) |
| `repeat` | weitere Erhoehung im 12-Uhr-Fenster am selben Tag, Kraftstoff und Tankstelle |

Eine frueh am Tag verbotene Erhoehung macht die erlaubte Erhoehung um 12 Uhr nicht nachtraeglich zum Verstoss.
Die Toleranz ist eine Annahme: das Gesetz sagt "um 12 Uhr", die Meldung an die MTS-K hat aber eine Verzoegerung.
Mit `--tolerance` laesst sich pruefen, wie stark das Ergebnis davon abhaengt.

**Wichtig:** Die Formate sind aus der Tankerkoenig-Dokumentation und oeffentlichen Projekten abgeleitet,
aber noch nicht an den echten Dateien geprueft (Zugang folgt). Bei Abweichungen (Zeitstempelformat,
Dateinamen) bitte eine Beispieldatei pruefen; die Parser stehen in `tankstellen/history.py`.

## Live-Abfrage (ohne historische Daten)

Die API liefert nur den aktuellen Preis, keinen Zeitpunkt der Aenderung. Daher wird regelmaessig abgefragt
und jede Abfrage mit der vorherigen verglichen. Eine Erhoehung hat irgendwann im Fenster
`(letzte Abfrage, aktuelle Abfrage]` stattgefunden:

| Ergebnis | Bedeutung |
|---|---|
| `violation` | Fenster enthaelt kein 12:00 (Berliner Zeit) -> Erhoehung kann nicht regelkonform gewesen sein |
| `ok_noon` | Fenster enthaelt 12:00 -> vermutlich regelkonform, zaehlt nicht als Verstoss |
| `multiple_per_day` | 2. (oder weitere) Erhoehung desselben Kraftstoffs am selben Tag |

Grenzen:
- Nur ab dem Start der Datensammlung, und nur wenn der Daemon laeuft. Abfragen mit mehr als 30 min Abstand werden nicht verglichen.
- Erhoehungen kurz vor/nach 12:00 innerhalb eines Abfragefensters sind nicht eindeutig und werden zugunsten der Tankstelle als `ok_noon` gewertet.
- Benzin = E5 und E10 getrennt ausgewiesen; Diesel separat.

Tabellen: `stations`, `price_observations` (jede Abfrage), `price_increases` (erkannte Erhoehungen + Urteil), `polls`,
`exact_changes` (historischer Import, exakte Zeitpunkte), `import_state`, `imported_files`.

Live-Daten und historische Daten werden getrennt ausgewertet; `report` zeigt beide, sofern vorhanden.
Der Daemon ist weiterhin sinnvoll, um die Luecke zwischen dem letzten CSV-Tag und heute zu fuellen.

## Tests

```powershell
python -m unittest discover -s tests
```

Datenquelle: Tankerkoenig.de / MTS-K, CC BY 4.0.
