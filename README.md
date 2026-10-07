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

## Wie Verstoesse erkannt werden

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

Tabellen: `stations`, `price_observations` (jede Abfrage), `price_increases` (erkannte Erhoehungen + Urteil), `polls`.

## Tests

```powershell
python -m unittest discover -s tests
```

Datenquelle: Tankerkoenig.de / MTS-K, CC BY 4.0.
