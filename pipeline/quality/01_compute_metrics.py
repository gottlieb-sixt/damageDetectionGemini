"""Berechnet Bildqualitäts-Metriken für alle exterior_photos.

Output: quality_manifest.parquet mit Spalten:
- path, plate, view, file_size, width, height
- sharpness (Laplacian-Varianz, höher = schärfer)
- brightness (0-255 mean)
- contrast (std-dev)
- saturation (mean S in HSV)
- overexposure_pct (% Pixel > 240)
- underexposure_pct (% Pixel < 15)
- aspect_ratio
- phash (64-bit perceptual hash für Dedup)
"""
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import imagehash
import numpy as np
import pandas as pd
from PIL import Image

EXTERIOR_DIR = Path(__file__).parent.parent.parent / "exterior_photos"
OUTPUT = Path(__file__).parent / "quality_manifest.parquet"

VIEWS = [
    "EXTERIOR_FRONT_STRAIGHT", "EXTERIOR_REAR_STRAIGHT",
    "DIAGONAL_FRONT_LEFT", "DIAGONAL_FRONT_RIGHT",
    "DIAGONAL_REAR_LEFT", "DIAGONAL_REAR_RIGHT",
    "TYRE_RIM_FRONT_LEFT", "TYRE_RIM_FRONT_RIGHT",
    "TYRE_RIM_REAR_LEFT", "TYRE_RIM_REAR_RIGHT",
]


def extract_view(filename):
    for v in VIEWS:
        if v in filename:
            return v
    if re.match(r"^[a-f0-9-]+_\d+\.jpg$", filename):
        return "exterior_legacy"
    return "UNKNOWN"


def compute_metrics(path_str):
    """Pro Bild alle Quality-Metriken berechnen."""
    path = Path(path_str)
    try:
        # Schneller Read mit cv2
        img = cv2.imread(str(path))
        if img is None:
            return {"path": path_str, "error": "cv2_read_failed"}
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # Sharpness — Varianz des Laplacian
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # Brightness + Contrast
        brightness = float(gray.mean())
        contrast = float(gray.std())

        # Saturation
        saturation = float(hsv[..., 1].mean())

        # Belichtungs-Clipping
        overexp = float((gray > 240).mean()) * 100
        underexp = float((gray < 15).mean()) * 100

        # pHash via PIL (CV2-img zu PIL)
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        phash_obj = imagehash.phash(pil_img, hash_size=16)  # 256-bit
        phash = str(phash_obj)

        return {
            "path": path_str,
            "plate": path.parent.name,
            "filename": path.name,
            "view": extract_view(path.name),
            "file_size": path.stat().st_size,
            "width": w,
            "height": h,
            "aspect_ratio": round(w / h, 3),
            "sharpness": round(sharpness, 1),
            "brightness": round(brightness, 1),
            "contrast": round(contrast, 1),
            "saturation": round(saturation, 1),
            "overexp_pct": round(overexp, 2),
            "underexp_pct": round(underexp, 2),
            "phash": phash,
        }
    except Exception as e:
        return {"path": path_str, "error": str(e)[:200]}


def collect_paths():
    paths = []
    for plate_dir in EXTERIOR_DIR.iterdir():
        if not plate_dir.is_dir():
            continue
        for f in plate_dir.iterdir():
            if f.suffix.lower() in (".jpg", ".jpeg"):
                paths.append(str(f))
    return paths


if __name__ == "__main__":
    print(f"Sammle Bilder aus {EXTERIOR_DIR} ...")
    paths = collect_paths()
    print(f"  {len(paths)} Bilder gefunden\n")

    n_workers = max(1, os.cpu_count() - 1)
    print(f"Berechne Metriken mit {n_workers} Workern ...")

    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=n_workers) as ex:
        futures = [ex.submit(compute_metrics, p) for p in paths]
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            results.append(r)
            if i % 1000 == 0 or i == len(paths):
                rate = i / (time.time() - t0)
                eta = (len(paths) - i) / rate
                print(f"  {i}/{len(paths)}  ({rate:.0f}/s, ETA {eta:.0f}s)", flush=True)

    df = pd.DataFrame(results)
    errs = df[df.get("error", pd.Series([None]*len(df))).notna()] if "error" in df.columns else pd.DataFrame()
    df_ok = df[~df.get("error", pd.Series([None]*len(df))).notna()] if "error" in df.columns else df

    df_ok.to_parquet(OUTPUT, index=False)
    print(f"\n✅ Manifest: {OUTPUT}")
    print(f"   OK: {len(df_ok)}, Errors: {len(errs)}")
    if len(errs) > 0:
        print(f"   Beispiel-Fehler: {errs.iloc[0]['error']}")
