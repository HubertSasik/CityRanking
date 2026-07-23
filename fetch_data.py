#!/usr/bin/env python3
"""
fetch_data.py
Holt Kennzahlen fuer deutsche Staedte aus drei APIs, berechnet einen
Attraktivitaets-Score und schreibt das Ergebnis als JSON fuer das Dashboard.

APIs (alle kostenlos, ohne Key):
  1. Bright Sky (DWD)   -> durchschnittliche Sonnenstunden        -> "weather"
  2. Overpass (OSM)     -> Kultur-/Freizeit-POIs + OEPNV-Haltestellen
                                                    -> "pois" und "transit"
  3. Open-Meteo         -> Luftqualitaet (European AQI)           -> "air"
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

# Gewichte der Kennzahlen (Summe egal, wird normalisiert)
WEIGHTS = {"weather": 0.25, "pois": 0.30, "transit": 0.25, "air": 0.20}

# Bei diesen Kennzahlen ist WENIGER besser -> Skala wird gedreht.
INVERT = {"air"}

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
# API 2: Overpass (OSM) -> Kultur-POIs UND OEPNV-Haltestellen in EINER Abfrage
# ---------------------------------------------------------------------------

def fetch_osm(city):
    r_, lat, lng = RADIUS_M, city["lat"], city["lng"]
    query = f"""
    [out:json][timeout:90];
    (
      node["amenity"~"restaurant|cafe|bar|theatre|cinema"](around:{r_},{lat},{lng});
      node["leisure"="park"](around:{r_},{lat},{lng});
      node["tourism"="museum"](around:{r_},{lat},{lng});
    )->.kultur;
    (
      node["highway"="bus_stop"](around:{r_},{lat},{lng});
      node["railway"~"station|halt|tram_stop"](around:{r_},{lat},{lng});
      node["public_transport"="platform"](around:{r_},{lat},{lng});
    )->.oepnv;
    .kultur out count;
    .oepnv out count;
    """
    for attempt in range(4):
        try:
            r = requests.post("https://overpass-api.de/api/interpreter",
                              data={"data": query}, headers=HEADERS, timeout=120)
            if r.status_code == 200:
                counts = [int(e["tags"]["total"])
                          for e in r.json().get("elements", [])
                          if e.get("type") == "count"]
                if len(counts) >= 2:
                    return {"pois": counts[0], "transit": counts[1]}
                print(f"  ! OSM unerwartete Antwort fuer {city['name']}: {r.text[:200]}")
                return {"pois": None, "transit": None}
            # 429/504 = gedrosselt/ueberlastet -> warten und erneut versuchen
            print(f"  ! OSM HTTP {r.status_code} fuer {city['name']} "
                  f"(Versuch {attempt + 1}/4): {r.text[:120]}")
            time.sleep(8 * (attempt + 1))
        except Exception as e:
            print(f"  ! OSM Fehler fuer {city['name']} (Versuch {attempt + 1}/4): {e}")
            time.sleep(8)
    return {"pois": None, "transit": None}


# ---------------------------------------------------------------------------
# API 3: Open-Meteo -> Luftqualitaet (European AQI, niedriger = besser)
# ---------------------------------------------------------------------------

def fetch_air(city):
    params = {"latitude": city["lat"], "longitude": city["lng"],
              "current": "european_aqi"}
    try:
        r = requests.get("https://air-quality-api.open-meteo.com/v1/air-quality",
                         params=params, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  ! Luft HTTP {r.status_code} fuer {city['name']}: {r.text[:150]}")
            return None
        return r.json().get("current", {}).get("european_aqi")
    except Exception as e:
        print(f"  ! Luft Fehler fuer {city['name']}: {e}")
        return None


# ---------------------------------------------------------------------------
# Normalisierung 0..100 (Min-Max)
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
        osm = fetch_osm(city)
        city["metrics"] = {
            "weather": fetch_weather(city),
            "pois": osm["pois"],
            "transit": osm["transit"],
            "air": fetch_air(city),
        }
        time.sleep(4)   # hoeflich zu Overpass sein

    print("\n--- Datenabdeckung ---")
    for key in WEIGHTS:
        ok = sum(1 for c in cities if c["metrics"][key] is not None)
        print(f"  {key:8s}: {ok}/{len(cities)} Staedte mit Daten")

    # normalisieren, invertierte Kennzahlen umdrehen
    normed = {}
    for key in WEIGHTS:
        vals = [c["metrics"][key] for c in cities]
        n = normalize(vals)
        if key in INVERT:
            n = [round(100 - x, 1) if vals[i] is not None else 0
                 for i, x in enumerate(n)]
        normed[key] = n

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
