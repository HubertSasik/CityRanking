#!/usr/bin/env python3
"""
fetch_data.py
Holt Kennzahlen fuer deutsche Staedte aus drei APIs, berechnet einen
Attraktivitaets-Score und schreibt das Ergebnis als JSON fuer das Dashboard.

Genutzte APIs (alle kostenlos, ohne API-Key):
  1. Bright Sky (DWD)    -> Wetter / durchschnittliche Sonnenstunden
  2. Overpass API (OSM)  -> Anzahl Freizeit-/Kultur-POIs
  3. DB transport.rest   -> Anzahl OEPNV-Haltestellen in der Naehe
"""

import json
import time
import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# EINSTELLUNGEN  --  hier passt du Pfade und Gewichte an
# ---------------------------------------------------------------------------

INPUT_PATH = Path("cities.json")            # deine hochgeladene Staedteliste
OUTPUT_PATH = Path("data/dashboard.json")   # Ergebnis fuer das Frontend

# Gewichte der drei Indikatoren (werden automatisch normalisiert).
# Hoeher = wichtiger fuer den Gesamt-Score. Aendere die Zahlen nach Belieben.
WEIGHTS = {
    "weather": 0.3,   # Sonnenstunden
    "pois": 0.4,      # Freizeit / Kultur
    "transit": 0.3,   # OEPNV-Anbindung
}

# Umkreis in Metern fuer POI- und Haltestellen-Suche
RADIUS_M = 3000

# HTTP-Header (die APIs moegen einen User-Agent)
HEADERS = {"User-Agent": "city-attractiveness-dashboard/1.0"}


# ---------------------------------------------------------------------------
# API 1: Bright Sky (DWD)  ->  durchschnittliche Sonnenstunden pro Tag
# ---------------------------------------------------------------------------

def fetch_weather(city):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=8)   # letzte 7 vollen Tage
    end = today - datetime.timedelta(days=1)
    params = {
        "lat": city["lat"],
        "lon": city["lng"],
        "date": start.isoformat(),
        "last_date": end.isoformat(),
    }
    try:
        r = requests.get("https://api.brightsky.dev/weather",
                         params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        records = r.json().get("weather", [])
    except Exception as e:
        print(f"  ! Wetter fehlgeschlagen fuer {city['name']}: {e}")
        return None
    if not records:
        return None
    # "sunshine" = Minuten Sonnenschein pro Stunde; ueber alle Stunden summieren
    total_min = sum((rec.get("sunshine") or 0) for rec in records)
    num_days = (end - start).days + 1
    return round(total_min / 60 / num_days, 2)   # Stunden Sonne pro Tag


# ---------------------------------------------------------------------------
# API 2: Overpass (OpenStreetMap)  ->  Anzahl Freizeit-/Kultur-POIs
# ---------------------------------------------------------------------------

def fetch_pois(city):
    query = f"""
    [out:json][timeout:60];
    (
      node["amenity"~"restaurant|cafe|bar|theatre|cinema"](around:{RADIUS_M},{city['lat']},{city['lng']});
      node["leisure"="park"](around:{RADIUS_M},{city['lat']},{city['lng']});
      node["tourism"="museum"](around:{RADIUS_M},{city['lat']},{city['lng']});
    );
    out count;
    """
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, headers=HEADERS, timeout=90)
        r.raise_for_status()
        elements = r.json().get("elements", [])
        if elements:
            return int(elements[0]["tags"]["total"])
    except Exception as e:
        print(f"  ! POIs fehlgeschlagen fuer {city['name']}: {e}")
    return None


# ---------------------------------------------------------------------------
# API 3: DB transport.rest  ->  Anzahl OEPNV-Haltestellen in der Naehe
# ---------------------------------------------------------------------------

def fetch_transit(city):
    params = {
        "latitude": city["lat"],
        "longitude": city["lng"],
        "results": 200,
        "distance": RADIUS_M,
        "stops": "true",
        "poi": "false",
    }
    try:
        r = requests.get("https://v6.db.transport.rest/locations/nearby",
                         params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        stops = r.json()
        return len(stops) if isinstance(stops, list) else None
    except Exception as e:
        print(f"  ! OEPNV fehlgeschlagen fuer {city['name']}: {e}")
    return None


# ---------------------------------------------------------------------------
# Hilfsfunktion: Werteliste auf 0..100 normalisieren (Min-Max)
# ---------------------------------------------------------------------------

def normalize(values):
    present = [v for v in values if v is not None]
    if not present:
        return [0 for _ in values]
    lo, hi = min(present), max(present)
    if hi == lo:  # alle gleich -> neutraler Mittelwert
        return [50 if v is not None else 0 for v in values]
    return [round((v - lo) / (hi - lo) * 100, 1) if v is not None else 0
            for v in values]


# ---------------------------------------------------------------------------
# Hauptablauf: einsammeln -> normalisieren -> Score -> speichern
# ---------------------------------------------------------------------------

def main():
    cities = json.loads(INPUT_PATH.read_text(encoding="utf-8"))

    # 1) Rohdaten pro Stadt aus allen drei APIs einsammeln
    for city in cities:
        print(f"Verarbeite {city['name']} ...")
        city["metrics"] = {
            "weather": fetch_weather(city),
            "pois": fetch_pois(city),
            "transit": fetch_transit(city),
        }
        time.sleep(2)  # hoeflich zu den kostenlosen APIs sein

    # 2) Jede Kennzahl auf 0..100 normalisieren
    normed = {
        key: normalize([c["metrics"][key] for c in cities])
        for key in WEIGHTS
    }

    # 3) Gewichteten Gesamt-Score berechnen
    total_weight = sum(WEIGHTS.values())
    for i, city in enumerate(cities):
        city["normalized"] = {k: normed[k][i] for k in WEIGHTS}
        score = sum(city["normalized"][k] * WEIGHTS[k] for k in WEIGHTS)
        city["score"] = round(score / total_weight, 1)

    # nach Score sortieren (bester zuerst)
    cities.sort(key=lambda c: c["score"], reverse=True)

    # 4) Ergebnis speichern
    result = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "weights": WEIGHTS,
        "cities": cities,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print(f"\nFertig -> {OUTPUT_PATH}")
    for c in cities:
        print(f"  {c['score']:5.1f}  {c['name']}")


if __name__ == "__main__":
    main()