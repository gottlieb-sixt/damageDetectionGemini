"""Per-Car-Quality-Filter: Top N Autos basierend auf aggregierter Bildqualität.

Strategie:
1. Pro Plate: aggregate Metriken (mean/median Quality, n_views, n_hard_fails)
2. Plates mit kompletter 10-View-Coverage werden bevorzugt
3. Cars-Score = Composite aus mean_quality, view_completeness, hard_fail_penalty
4. Top N Cars werden behalten — ALLE ihre exterior_photos sind im Output
"""
import base64
import io
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import imagehash
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image

MANIFEST = Path(__file__).parent / "quality_manifest.parquet"
DAMAGE_CASES = Path(__file__).parent.parent.parent / "damage_cases.json"
OUT_MANIFEST = Path(__file__).parent / "filtered_cars.parquet"
OUT_IMAGE_LIST = Path(__file__).parent / "selected_images.parquet"
OUT_HTML = Path(__file__).parent / "car_quality_report.html"

TARGET_N_CARS = 500
EXPECTED_VIEWS = [
    "EXTERIOR_FRONT_STRAIGHT", "EXTERIOR_REAR_STRAIGHT",
    "DIAGONAL_FRONT_LEFT", "DIAGONAL_FRONT_RIGHT",
    "DIAGONAL_REAR_LEFT", "DIAGONAL_REAR_RIGHT",
    "TYRE_RIM_FRONT_LEFT", "TYRE_RIM_FRONT_RIGHT",
    "TYRE_RIM_REAR_LEFT", "TYRE_RIM_REAR_RIGHT",
]


# === Quality-Score-Berechnung (aus 02_filter) ===
def normalize(series, lo, hi, invert=False):
    p_lo, p_hi = series.quantile([lo/100, hi/100])
    norm = ((series - p_lo) / max(p_hi - p_lo, 1e-9)).clip(0, 1)
    return 1 - norm if invert else norm


def composite_image_score(df):
    return (100 * (
        0.35 * normalize(df["sharpness"], 5, 95) +
        0.20 * normalize(df["contrast"], 5, 95) +
        0.10 * normalize(df["saturation"], 5, 95) +
        0.10 * normalize(np.minimum(df["width"], df["height"]), 5, 95) +
        0.15 * (1 - normalize(df["overexp_pct"], 5, 95)) +
        0.10 * (1 - normalize(df["underexp_pct"], 5, 95))
    )).round(1)


def hard_fail(row):
    reasons = []
    if min(row["width"], row["height"]) < 800: reasons.append("low_resolution")
    if row["sharpness"] < 30: reasons.append("very_blurry")
    if row["brightness"] < 40: reasons.append("too_dark")
    if row["brightness"] > 220: reasons.append("too_bright")
    if row["overexp_pct"] > 10: reasons.append("burnt_out")
    if row["underexp_pct"] > 30: reasons.append("black_out")
    if row["saturation"] < 10: reasons.append("monochrome")
    return reasons


def safe(name): return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def aggregate_per_car(df, damage_data):
    """Pro Plate: aggregate Metriken."""
    cars = []
    for plate, group in df.groupby("plate"):
        n_imgs = len(group)
        unique_views = set(group["view"]) - {"exterior_legacy", "UNKNOWN"}
        n_unique_views = len(unique_views)
        completeness = n_unique_views / len(EXPECTED_VIEWS)

        n_hard_fail = int(group["is_hard_fail"].sum())
        hard_fail_pct = n_hard_fail / n_imgs

        # Quality-Stats nur über non-hard-fail Bilder
        clean = group[~group["is_hard_fail"]]
        if len(clean) > 0:
            mean_quality = clean["quality_score"].mean()
            median_quality = clean["quality_score"].median()
            min_quality = clean["quality_score"].min()
        else:
            mean_quality = median_quality = min_quality = 0

        # Damage-Info aus damage_cases.json
        # Plate-Name in damage_cases ist mit Leerzeichen, in path ohne -> reverse lookup nötig
        damage_info = None
        for orig_plate in damage_data:
            if safe(orig_plate) == plate:
                damage_info = damage_data[orig_plate]
                break
        if damage_info:
            n_damages = sum(len(c.get("damages", [])) for c in damage_info.get("damage_cases", []))
            n_cases = len(damage_info.get("damage_cases", []))
            orig_plate_name = next(p for p in damage_data if safe(p) == plate)
        else:
            n_damages = n_cases = 0
            orig_plate_name = plate

        cars.append({
            "plate_safe": plate,
            "plate_original": orig_plate_name,
            "n_images": n_imgs,
            "n_unique_views": n_unique_views,
            "completeness": completeness,
            "n_hard_fail": n_hard_fail,
            "hard_fail_pct": hard_fail_pct,
            "mean_quality": round(mean_quality, 1),
            "median_quality": round(median_quality, 1),
            "min_quality": round(min_quality, 1),
            "n_damages": n_damages,
            "n_damage_cases": n_cases,
        })
    return pd.DataFrame(cars)


def car_score(cars_df):
    """Composite Car-Score (0-100)."""
    # Komponenten:
    # 1. Mean Image Quality (40%) — Bildqualität
    # 2. Completeness (30%) — alle 10 Winkel vorhanden?
    # 3. Hard-Fail-Penalty (15%) — wenig kaputte Bilder
    # 4. Min-Quality (15%) — schwächstes Bild nicht zu schlecht
    s = (
        0.40 * normalize(cars_df["mean_quality"], 5, 95) +
        0.30 * cars_df["completeness"] +
        0.15 * (1 - normalize(cars_df["hard_fail_pct"], 5, 95)) +
        0.15 * normalize(cars_df["min_quality"], 5, 95)
    )
    return (s * 100).round(1)


def make_car_score_hist(cars_df, threshold):
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(cars_df["car_score"], bins=60, color="#ff5f00", edgecolor="white", linewidth=0.3)
    ax.axvline(threshold, color="#10b981", linewidth=2, linestyle="--", label=f"Threshold: {threshold:.0f}")
    ax.set_xlabel("Car Quality Score")
    ax.set_ylabel("Anzahl Autos")
    ax.set_title(f"Car-Score-Verteilung — Threshold für Top-{TARGET_N_CARS}")
    ax.legend()
    ax.grid(alpha=0.3)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close()
    return base64.b64encode(buf.getvalue()).decode()


def render_car_card(row, image_paths):
    """Zeigt 1 Auto mit allen seinen exterior_photos."""
    plate_orig = row["plate_original"]
    score = row["car_score"]
    n_imgs = row["n_images"]
    n_views = row["n_unique_views"]
    n_hf = row["n_hard_fail"]
    n_dmg = row["n_damages"]

    thumb_html = ""
    for p in sorted(image_paths)[:12]:  # Max 12 Thumbs pro Karte
        try:
            img = Image.open(p).convert("RGB")
            if max(img.size) > 200:
                r = 200 / max(img.size)
                img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=60)
            b64 = base64.b64encode(buf.getvalue()).decode()
            view = next((v for v in EXPECTED_VIEWS if v in os.path.basename(p)), "legacy")
            short = view.replace("EXTERIOR_", "").replace("DIAGONAL_", "DIAG_").replace("TYRE_RIM_", "TYR_")[:10]
            thumb_html += f'<div class="thumb"><img src="data:image/jpeg;base64,{b64}"/><span>{short}</span></div>'
        except Exception:
            pass

    return f"""
<div class="car">
  <header>
    <h3>{plate_orig}</h3>
    <div class="meta">
      <span><strong>{score:.0f}</strong>/100 score</span>
      <span>{n_views}/10 views</span>
      <span>{n_imgs} bilder</span>
      <span>{n_hf} hard fails</span>
      <span>{n_dmg} DB-damages</span>
    </div>
  </header>
  <div class="thumbs">{thumb_html}</div>
</div>"""


if __name__ == "__main__":
    print(f"Lade {MANIFEST} ...")
    df = pd.read_parquet(MANIFEST)
    df["quality_score"] = composite_image_score(df)
    df["hard_fail_reasons"] = df.apply(hard_fail, axis=1)
    df["is_hard_fail"] = df["hard_fail_reasons"].apply(len) > 0
    print(f"  {len(df)} Bilder, {df['plate'].nunique()} unique Plates")

    print(f"\nLade damage_cases.json ...")
    with open(DAMAGE_CASES) as f:
        damage_data = json.load(f)

    print(f"\nAggregiere pro Auto ...")
    cars = aggregate_per_car(df, damage_data)
    cars["car_score"] = car_score(cars)
    cars_sorted = cars.sort_values("car_score", ascending=False).reset_index(drop=True)

    # Top N
    threshold = cars_sorted.iloc[min(TARGET_N_CARS - 1, len(cars_sorted) - 1)]["car_score"]
    cars_sorted["selected"] = cars_sorted["car_score"] >= threshold
    n_selected = cars_sorted["selected"].sum()

    print(f"\n📊 Car-Filter-Ergebnis:")
    print(f"   Total Autos:        {len(cars)}")
    print(f"   Score-Threshold:    {threshold:.1f}")
    print(f"   Ausgewählt:         {n_selected} (Ziel: {TARGET_N_CARS})")
    print(f"\n   Top-5:")
    for _, r in cars_sorted.head(5).iterrows():
        print(f"     {r['plate_original']:<20} score={r['car_score']:.0f} views={r['n_unique_views']}/10 imgs={r['n_images']} damages={r['n_damages']}")
    print(f"\n   Threshold-Border (an der Grenze):")
    border_idx = TARGET_N_CARS - 1
    for _, r in cars_sorted.iloc[max(0, border_idx-2):border_idx+3].iterrows():
        print(f"     {r['plate_original']:<20} score={r['car_score']:.0f} views={r['n_unique_views']}/10 imgs={r['n_images']} damages={r['n_damages']}")

    # Bilder pro ausgewähltem Auto sammeln
    selected_plates = set(cars_sorted[cars_sorted["selected"]]["plate_safe"])
    selected_images = df[df["plate"].isin(selected_plates)].copy()
    selected_images["selected"] = True

    print(f"\n   Gesamt-Bilder im Annotation-Pool: {len(selected_images)} (∅ {len(selected_images)/n_selected:.1f}/Auto)")
    print(f"   Davon hard_fail: {selected_images['is_hard_fail'].sum()} (werden bei Annotation skippt)")

    # Speichern
    cars_sorted.to_parquet(OUT_MANIFEST, index=False)
    selected_images[["path", "plate", "view", "quality_score", "is_hard_fail"]].to_parquet(OUT_IMAGE_LIST, index=False)
    print(f"\n✅ Cars-Manifest:  {OUT_MANIFEST}")
    print(f"✅ Image-List:     {OUT_IMAGE_LIST}")

    # HTML
    print("Erzeuge HTML-Report ...")
    hist_b64 = make_car_score_hist(cars_sorted, threshold)
    # Bilder pro Plate
    images_by_plate = defaultdict(list)
    for _, r in selected_images.iterrows():
        images_by_plate[r["plate"]].append(r["path"])

    # Top-10 ausgewählt + Bottom-5 ausgewählt (Grenzfälle) + Top-5 abgelehnt
    top_cards = []
    for _, r in cars_sorted.head(10).iterrows():
        top_cards.append(render_car_card(r, images_by_plate[r["plate_safe"]]))

    border_cards = []
    borderline_keep = cars_sorted[cars_sorted["selected"]].tail(5)
    for _, r in borderline_keep.iterrows():
        border_cards.append(render_car_card(r, images_by_plate[r["plate_safe"]]))

    rejected_cards = []
    rejected_top = cars_sorted[~cars_sorted["selected"]].head(5)
    # Bilder von abgelehnten Autos
    for _, r in rejected_top.iterrows():
        imgs = df[df["plate"] == r["plate_safe"]]["path"].tolist()
        rejected_cards.append(render_car_card(r, imgs))

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Per-Car Quality Filter Report</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f4f4f7; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 24px; }}
header.top {{ background: white; border-left: 5px solid #ff5f00; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
header.top h1 {{ margin: 0 0 8px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 16px; }}
.stat {{ background: #f6f7f9; padding: 12px 16px; border-radius: 8px; }}
.stat .label {{ font-size: 11px; text-transform: uppercase; color: #5a6270; }}
.stat .value {{ font-size: 24px; font-weight: 700; }}
.section {{ background: white; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.car {{ background: #f9fafb; padding: 16px; border-radius: 10px; margin-bottom: 14px; }}
.car header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }}
.car h3 {{ margin: 0; font-family: monospace; color: #ff5f00; }}
.car .meta {{ display: flex; gap: 12px; font-size: 12px; }}
.car .meta span {{ background: #fff; padding: 3px 10px; border-radius: 4px; border: 1px solid #e2e5ea; }}
.thumbs {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; }}
.thumb {{ position: relative; }}
.thumb img {{ width: 100%; border-radius: 4px; }}
.thumb span {{ position: absolute; bottom: 2px; left: 2px; background: rgba(0,0,0,.7); color: white; font-size: 9px; padding: 1px 5px; border-radius: 3px; font-family: monospace; }}
img.hist {{ max-width: 100%; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #e2e5ea; }}
th {{ background: #f6f7f9; }}
</style></head><body>
<div class="container">
  <header class="top">
    <h1>🚗 Per-Car Quality Filter — Top {TARGET_N_CARS} Autos</h1>
    <p>Aggregate Auto-Qualität über alle Winkel: 40% mean_quality + 30% view_completeness + 15% min_quality + 15% (1 - hard_fail_penalty)</p>
    <div class="stats">
      <div class="stat"><div class="label">Total Autos</div><div class="value">{len(cars):,}</div></div>
      <div class="stat"><div class="label">Ausgewählt</div><div class="value" style="color:#10b981">{n_selected}</div></div>
      <div class="stat"><div class="label">Score-Threshold</div><div class="value">{threshold:.0f}</div></div>
      <div class="stat"><div class="label">Bilder im Pool</div><div class="value">{len(selected_images):,}</div></div>
      <div class="stat"><div class="label">Ø Bilder/Auto</div><div class="value">{len(selected_images)/max(n_selected,1):.1f}</div></div>
      <div class="stat"><div class="label">Hard-Fails (~skippable)</div><div class="value">{selected_images['is_hard_fail'].sum()}</div></div>
    </div>
  </header>

  <div class="section">
    <h2>Car-Score-Verteilung</h2>
    <img class="hist" src="data:image/png;base64,{hist_b64}"/>
  </div>

  <div class="section">
    <h2>🏆 Top-10 Autos (höchster Car-Score)</h2>
    {''.join(top_cards)}
  </div>

  <div class="section">
    <h2>⚖️ Borderline-Keeps (gerade so noch drin — Bottom 5 der ausgewählten)</h2>
    {''.join(border_cards)}
  </div>

  <div class="section">
    <h2>❌ Borderline-Rejected (gerade so rausgeflogen — Top 5 der abgelehnten)</h2>
    {''.join(rejected_cards)}
  </div>
</div></body></html>
"""

    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"✅ Report:        {OUT_HTML}")
