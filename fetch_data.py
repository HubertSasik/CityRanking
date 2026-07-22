#!/usr/bin/env python3
"""
fetch_data.py
Holt Kennzahlen fuer deutsche Staedte aus drei APIs, berechnet einen
Attraktivitaets-Score und schreibt das Ergebnis als JSON fuer das Dashboard.

APIs (kostenlos, ohne Key):
  1. Bright Sky (DWD)    -> durchschnittliche Sonnenstunden
  2. Overpass API (OSM)  -> Anzahl Freizeit-/Kultur-POIs
  3. DB transport.rest   -> Anzahl OEPNV-Haltestellen in der Naehe

Diese Version gibt bei jedem Fehlschlag HTTP-Status und Serverantwort aus,
damit man im Actions-Log genau sieht, WARUM eine API nichts liefert.
"""

import json
import time
import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# EINSTELLUNGEN
# ---------------------------------------------------------------------------

INPUT_PATH = Path("cities.json")
OUTPUT_PATH = Path("data/dashboard.json")

WEIGHTS = {"weather": 0.3, "pois": 0.4, "transit": 0.3}
RADIUS_M = 3000
HEADERS = {"User-Agent": "city-attractiveness-dashboard/1.0"}


# ---------------------------------------------------------------------------
# API 1: Bright Sky (DWD) -> Sonnenstunden pro Tag
# ---------------------------------------------------------------------------

def fetch_weather(city):
    today = datetime.date.today()
    start = today - datetime.timedelta(days=8)
    end = today - datetime.timedelta(days=1)
    params = {"lat": city["lat"], "lon": city["lng"],
              "date": start.isoformat(), "last_date": end.isoformat()}
    try:
        r = requests.get("https://api.brightsky.dev/weather",
                         params=params, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  ! Wetter HTTP {r.status_code} fuer {city['name']}: {r.text[:150]}")
            return None
        records = r.json().get("weather", [])
    except Exception as e:
        print(f"  ! Wetter Fehler fuer {city['name']}: {e}")
        return None
    if not records:
        return None
    total_min = sum((rec.get("sunshine") or 0) for rec in records)
    num_days = (end - start).days + 1
    return round(total_min / 60 / num_days, 2)


# ---------------------------------------------------------------------------
# API 2: Overpass (OSM) -> Anzahl Freizeit-/Kultur-POIs  (mit Retry + Backoff)
# ---------------------------------------------------------------------------

def fetch_pois(city):
    query = f"""
    [out:json][timeout:90];
    (
      node["amenity"~"restaurant|cafe|bar|theatre|cinema"](around:{RADIUS_M},{city['lat']},{city['lng']});
      node["leisure"="park"](around:{RADIUS_M},{city['lat']},{city['lng']});
      node["tourism"="museum"](around:{RADIUS_M},{city['lat']},{city['lng']});
    );
    out count;
    """
    for attempt in range(3):
        try:
            r = requests.post("https://overpass-api.de/api/interpreter",
                              data={"data": query}, headers=HEADERS, timeout=120)
            if r.status_code == 200:
                elements = r.json().get("elements", [])
                if elements and "tags" in elements[0]:
                    return int(elements[0]["tags"]["total"])
                print(f"  ! POIs unerwartete Antwort fuer {city['name']}: {r.text[:200]}")
                return None
            print(f"  ! POIs HTTP {r.status_code} fuer {city['name']} "
                  f"(Versuch {attempt + 1}/3): {r.text[:150]}")
            time.sleep(6 * (attempt + 1))
        except Exception as e:
            print(f"  ! POIs Fehler fuer {city['name']} (Versuch {attempt + 1}/3): {e}")
            time.sleep(6)
    return None


# ---------------------------------------------------------------------------
# API 3: DB transport.rest -> Anzahl OEPNV-Haltestellen in der Naehe
# ---------------------------------------------------------------------------

def fetch_transit(city):
    params = {"latitude": city["lat"], "longitude": city["lng"],
              "results": 100, "distance": RADIUS_M, "stops": "true", "poi": "false"}
    try:
        r = requests.get("https://v6.db.transport.rest/locations/nearby",
                         params=params, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  ! OEPNV HTTP {r.status_code} fuer {city['name']}: {r.text[:200]}")
            return None
        stops = r.json()
        if isinstance(stops, list):
            return len(stops)
        print(f"  ! OEPNV unerwartete Antwort fuer {city['name']}: {str(stops)[:200]}")
    except Exception as e:
        print(f"  ! OEPNV Fehler fuer {city['name']}: {e}")
    return None


# ---------------------------------------------------------------------------
# Normalisierung 0..100 (Min-Max). Niedrigster Wert -> 0, hoechster -> 100.
# ---------------------------------------------------------------------------

def normalize(values):
    present = [v for v in values if v is not None]
    if not present:
        return [0 for _ in values]
    lo, hi = min(present), max(present)
    if hi == lo:
        return [50 if v is not None else 0 for v in values]
    return [round((v - lo) / (hi - lo) * 100, 1) if v is not None else 0
            for v in values]


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def main():
    if not INPUT_PATH.exists():
        raise SystemExit(f"cities.json nicht gefunden unter {INPUT_PATH.resolve()}")
    cities = json.loads(INPUT_PATH.read_text(encoding="utf-8"))

    for city in cities:
        print(f"Verarbeite {city['name']} ...")
        city["metrics"] = {
            "weather": fetch_weather(city),
            "pois": fetch_pois(city),
            "transit": fetch_transit(city),
        }
        time.sleep(2)

    print("\n--- Datenabdeckung ---")
    for key in WEIGHTS:
        ok = sum(1 for c in cities if c["metrics"][key] is not None)
        print(f"  {key:8s}: {ok}/{len(cities)} Staedte mit Daten")

    normed = {key: normalize([c["metrics"][key] for c in cities]) for key in WEIGHTS}

    total_weight = sum(WEIGHTS.values())
    for i, city in enumerate(cities):
        city["normalized"] = {k: normed[k][i] for k in WEIGHTS}
        score = sum(city["normalized"][k] * WEIGHTS[k] for k in WEIGHTS)
        city["score"] = round(score / total_weight, 1)

    cities.sort(key=lambda c: c["score"], reverse=True)

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