"""Lädt alle Schadensfotos parallel.
Struktur: damage_photos/<KENNZEICHEN>/<CASE_ID>/<TYPE>__<PHOTO_ID>.jpeg
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

OUT_DIR = "damage_photos"
MAX_WORKERS = 50

def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")

def build_jobs(data):
    jobs = []
    for plate, payload in data.items():
        plate_safe = safe(plate)
        for case in payload.get("damage_cases", []):
            cid = case.get("damage_case_id", "unknown")
            for dmg in case.get("damages", []):
                for coord in dmg.get("coordinates", []):
                    for p in coord.get("photos", []):
                        pid = p.get("photo_id", "")
                        ptype = p.get("type", "UNKNOWN")
                        url = p.get("url")
                        if not url or not pid:
                            continue
                        dst = os.path.join(OUT_DIR, plate_safe, cid, f"{ptype}__{pid}.jpeg")
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
    with open("damage_cases.json") as f:
        data = json.load(f)
    jobs = build_jobs(data)
    # Sortiere nach ältester URL (X-Amz-Date) zuerst, damit die zuerst ablaufenden zuerst gezogen werden
    def amz_date(url):
        m = re.search(r"X-Amz-Date=(\d{8}T\d{6}Z)", url)
        return m.group(1) if m else "99999999T999999Z"
    jobs.sort(key=lambda j: amz_date(j[0]))

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
            if i % 500 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  ok={ok}  skip={skip}  err={err}", flush=True)

    print(f"\n✅ Fertig: {ok} heruntergeladen, {skip} übersprungen, {err} Fehler")
    if errors[:3]:
        print("Erste Fehler:")
        for e in errors[:3]:
            print(f"  - {e}")
