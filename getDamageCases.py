"""Holt Schadensfälle pro license_plate aus VehicleDamage/GetDamageCasesByVehicle.
Resumebar: speichert nach jedem Fahrzeug in damage_cases.json.
Bei abgelaufenem Token: Token in getPictures.py erneuern und Skript erneut starten.
"""
import json
import os
import time
import sys
import requests
from getPictures import TOKEN

URL = "https://grpc-query-tool-prod.orange.sixt.com/com.sixt.service.vehicle_damage.api.VehicleDamage/GetDamageCasesByVehicle"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://lynx.orange.sixt.com",
}

OUT = "damage_cases.json"
SRC = "photos_export.json"

def fetch_damage(plate: str):
    payload = {
        "license_plate": plate,
        "include_repaired_damages": False,
        "fetch_all_damage_cases": True,
        "include_pictogram": False,
        "include_default_damages": False,
        "include_inactive_damages": False,
    }
    r = requests.post(URL, json=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    # Quellplates
    with open(SRC) as f:
        src = json.load(f)
    plates = sorted({v["license_plate"] for v in src})

    # Bisheriger Stand
    if os.path.exists(OUT):
        with open(OUT) as f:
            results = json.load(f)
    else:
        results = {}
    done = set(results.keys())
    todo = [p for p in plates if p not in done]
    print(f"Plates total: {len(plates)} | bereits: {len(done)} | offen: {len(todo)}")

    consecutive_errors = 0
    for i, plate in enumerate(todo, 1):
        try:
            data = fetch_damage(plate)
            results[plate] = data
            consecutive_errors = 0
        except requests.HTTPError as e:
            consecutive_errors += 1
            print(f"  ✗ {plate}: {e}")
            if e.response is not None and e.response.status_code == 401:
                print("\n⚠️  401 Unauthorized — Token abgelaufen. Stand gespeichert. Token erneuern und neu starten.")
                break
            if consecutive_errors >= 5:
                print("\n⚠️  5 Fehler hintereinander — Abbruch. Stand gespeichert.")
                break
        except Exception as e:
            consecutive_errors += 1
            print(f"  ✗ {plate}: {e}")

        # Inkrementell speichern (alle 20 Plates)
        if i % 20 == 0 or i == len(todo):
            with open(OUT, "w") as f:
                json.dump(results, f, indent=2)
            cases = sum(len(v.get("damage_cases", [])) for v in results.values())
            print(f"  {i}/{len(todo)}  fertig: {len(results)}  Cases: {cases}")
        time.sleep(0.2)

    # Final speichern
    with open(OUT, "w") as f:
        json.dump(results, f, indent=2)
    cases = sum(len(v.get("damage_cases", [])) for v in results.values())
    print(f"\n✅ Stand: {len(results)}/{len(plates)} Plates abgefragt, {cases} Damage-Cases gesamt → {OUT}")
