"""Lädt alle exterior-Fotos parallel in ./exterior_photos/ herunter.
Struktur: exterior_photos/<KENNZEICHEN>/<TASK_ID>_<INDEX>.jpg
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

OUT_DIR = "exterior_photos"
MAX_WORKERS = 32

# Altes Schema (vor Mai 2026): "exterior"
# Neues Schema (ab Mai 2026): granulare Typen
EXTERIOR_TYPES = {
    "exterior",
    "EXTERIOR_FRONT_STRAIGHT", "EXTERIOR_REAR_STRAIGHT",
    "DIAGONAL_FRONT_LEFT", "DIAGONAL_FRONT_RIGHT",
    "DIAGONAL_REAR_LEFT", "DIAGONAL_REAR_RIGHT",
    "TYRE_RIM_FRONT_LEFT", "TYRE_RIM_FRONT_RIGHT",
    "TYRE_RIM_REAR_LEFT", "TYRE_RIM_REAR_RIGHT",
}

def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")

def build_jobs(data):
    jobs = []
    for v in data:
        plate = safe(v["license_plate"])
        for t in v.get("tasks", []):
            tid = t["task_id"]
            for p in t.get("photos", []):
                ptype = p.get("type")
                if ptype not in EXTERIOR_TYPES:
                    continue
                for idx, url in enumerate(p.get("urls", [])):
                    # Altes Schema behält Original-Namen (damit bereits geladene Dateien matchen)
                    if ptype == "exterior":
                        fname = f"{tid}_{idx}.jpg"
                    else:
                        fname = f"{tid}_{ptype}_{idx}.jpg"
                    dst = os.path.join(OUT_DIR, plate, fname)
                    jobs.append((url, dst))
    return jobs

def download(url_dst):
    url, dst = url_dst
    if os.path.exists(dst):
        return ("skip", dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with open(dst, "wb") as f:
            f.write(r.content)
        return ("ok", dst)
    except Exception as e:
        return ("err", f"{dst}: {e}")

if __name__ == "__main__":
    with open("photos_export.json") as f:
        data = json.load(f)
    jobs = build_jobs(data)
    print(f"Starte {len(jobs)} Downloads mit {MAX_WORKERS} Threads ...")

    ok = skip = err = 0
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(download, j) for j in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            status, info = fut.result()
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                err += 1
                errors.append(info)
            if i % 200 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  ok={ok}  skip={skip}  err={err}")

    print(f"\n✅ Fertig: {ok} heruntergeladen, {skip} übersprungen, {err} Fehler")
    if errors[:5]:
        print("Erste Fehler:")
        for e in errors[:5]:
            print(f"  - {e}")
