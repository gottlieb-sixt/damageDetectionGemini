"""Lockerer Filter + Damage-Type-Stratifizierung.

Stufe 1: Score-Filter — Top 60% Autos (statt 35%)
Stufe 2: Stratifizierung — pick ~500 mit balancierter Damage-Verteilung

Ziel-Verteilung (oversample rare classes für besseres Training):
  scratch:    150
  stone_chip: 100
  dent:        75
  crack:       50  (oversampled vs. 1.3% natural)
  missing:     50  (oversampled vs. 1.4% natural)
  major:       25
  other:       50
  → Total ~500
"""
import base64
import io
import json
import re
from collections import defaultdict, Counter
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# Reuse from 03
import sys
sys.path.insert(0, str(Path(__file__).parent))
from importlib import import_module
import importlib.util
spec = importlib.util.spec_from_file_location("base_filter", Path(__file__).parent / "03_filter_by_car.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

MANIFEST = Path(__file__).parent / "quality_manifest.parquet"
DAMAGE_CASES = Path(__file__).parent.parent.parent / "damage_cases.json"
OUT_MANIFEST = Path(__file__).parent / "stratified_cars.parquet"
OUT_IMAGE_LIST = Path(__file__).parent / "stratified_images.parquet"
OUT_HTML = Path(__file__).parent / "stratified_report.html"

KEEP_TOP_PERCENT = 60  # Stufe 1: gelockert

# Stufe 2: Stratifizierungs-Ziele (oversample rare classes)
STRATA_TARGETS = {
    "scratch":    150,
    "stone_chip": 100,
    "dent":        75,
    "crack":       50,
    "missing":     50,
    "major":       25,
    "other":       50,
}
# Total: 500

TYPE_MAP = {
    "TYPE_SCRATCH": "scratch", "TYPE_STONE_CHIP": "stone_chip",
    "TYPE_STONE_CHIP_WITH_CRACK": "stone_chip", "TYPE_DENT": "dent",
    "TYPE_DENTED": "dent", "TYPE_CRACK": "crack", "TYPE_HOLE": "crack",
    "TYPE_BROKEN": "crack", "TYPE_MISSING": "missing", "TYPE_LOOSE": "missing",
    "TYPE_CRASH": "major", "TYPE_HAIL_DAMAGE": "major",
}


def safe(name): return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def get_car_damage_types(plate_orig, damage_data):
    """Pro Auto: Set aller Master-Classes in den Damages."""
    payload = damage_data.get(plate_orig, {})
    classes = set()
    for case in payload.get("damage_cases", []):
        for dmg in case.get("damages", []):
            cls = TYPE_MAP.get(dmg.get("type"), "other")
            classes.add(cls)
    return classes


def stratified_pick(eligible_cars_df, damage_data, targets):
    """Greedy-Pick: iteriere von rarest → most common, picke best-score Auto."""
    # Pro Auto die Damage-Klassen
    eligible_cars_df = eligible_cars_df.copy()
    eligible_cars_df["damage_classes"] = eligible_cars_df["plate_original"].apply(
        lambda p: get_car_damage_types(p, damage_data)
    )

    selected = set()
    coverage = Counter()
    # Sortiere Klassen von rarest → most common (rare als erste picken)
    classes_in_order = sorted(targets.keys(), key=lambda c: targets[c])

    for cls in classes_in_order:
        target = targets[cls]
        remaining = target - coverage[cls]
        if remaining <= 0:
            continue
        # Kandidaten: Autos die diese Klasse haben + noch nicht selected
        candidates = eligible_cars_df[
            (eligible_cars_df["plate_safe"].apply(lambda p: p not in selected)) &
            (eligible_cars_df["damage_classes"].apply(lambda s: cls in s))
        ].sort_values("car_score", ascending=False)

        picked = 0
        for _, row in candidates.iterrows():
            if picked >= remaining:
                break
            plate = row["plate_safe"]
            if plate in selected:
                continue
            selected.add(plate)
            # Erhöhe Coverage für ALLE Klassen die dieses Auto hat
            for c in row["damage_classes"]:
                coverage[c] += 1
            picked += 1

        print(f"  {cls:<12} ziel={target:<3} → +{picked} (coverage jetzt: {coverage[cls]})")

    # Falls noch nicht 500: Auffüllen mit besten verbleibenden (egal welche Klasse)
    total_target = sum(targets.values())
    while len(selected) < total_target:
        candidates = eligible_cars_df[
            eligible_cars_df["plate_safe"].apply(lambda p: p not in selected)
        ].sort_values("car_score", ascending=False)
        if len(candidates) == 0:
            break
        plate = candidates.iloc[0]["plate_safe"]
        selected.add(plate)
        for c in candidates.iloc[0]["damage_classes"]:
            coverage[c] += 1

    return selected, coverage


def make_diversity_chart(coverage, targets):
    fig, ax = plt.subplots(figsize=(10, 4))
    classes = list(targets.keys())
    actual = [coverage.get(c, 0) for c in classes]
    target_vals = [targets[c] for c in classes]
    x = np.arange(len(classes))
    w = 0.4
    ax.bar(x - w/2, target_vals, w, label="Ziel", color="#cbd5e1")
    ax.bar(x + w/2, actual, w, label="Erreicht", color="#ff5f00")
    ax.set_xticks(x)
    ax.set_xticklabels(classes, rotation=20)
    ax.set_ylabel("Anzahl Autos mit dieser Klasse")
    ax.set_title("Damage-Type-Coverage in 500-Auto-Pool")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close()
    return base64.b64encode(buf.getvalue()).decode()


if __name__ == "__main__":
    print(f"Lade {MANIFEST} ...")
    df = pd.read_parquet(MANIFEST)
    df["quality_score"] = bf.composite_image_score(df)
    df["hard_fail_reasons"] = df.apply(bf.hard_fail, axis=1)
    df["is_hard_fail"] = df["hard_fail_reasons"].apply(len) > 0
    print(f"  {len(df)} Bilder")

    print(f"Lade damage_cases.json ...")
    with open(DAMAGE_CASES) as f:
        damage_data = json.load(f)

    print(f"Aggregiere pro Auto ...")
    cars = bf.aggregate_per_car(df, damage_data)
    cars["car_score"] = bf.car_score(cars)
    cars_sorted = cars.sort_values("car_score", ascending=False).reset_index(drop=True)

    # Stufe 1: Top 60%
    n_eligible_target = int(len(cars_sorted) * KEEP_TOP_PERCENT / 100)
    eligible = cars_sorted.head(n_eligible_target).copy()
    threshold = eligible["car_score"].min()
    print(f"\n📊 Stufe 1: Top {KEEP_TOP_PERCENT}% = {len(eligible)} Autos (Score >= {threshold:.1f})")

    # Stufe 2: Stratifizieren
    print(f"\n📊 Stufe 2: Damage-Type-Stratifizierung")
    selected_plates, coverage = stratified_pick(eligible, damage_data, STRATA_TARGETS)
    print(f"\n   Final: {len(selected_plates)} Autos ausgewählt")

    cars_sorted["stratified_selected"] = cars_sorted["plate_safe"].isin(selected_plates)
    n_sel = cars_sorted["stratified_selected"].sum()

    # Bilder pro Auto
    selected_images = df[df["plate"].isin(selected_plates)].copy()
    selected_images["selected"] = True
    print(f"\n   Bilder im Pool:    {len(selected_images)} ({len(selected_images)/n_sel:.1f}/Auto)")
    print(f"   Saubere Bilder:    {(~selected_images['is_hard_fail']).sum()}")

    # Speichern
    cars_sorted.to_parquet(OUT_MANIFEST, index=False)
    selected_images[["path", "plate", "view", "quality_score", "is_hard_fail"]].to_parquet(OUT_IMAGE_LIST, index=False)
    print(f"\n✅ Cars-Manifest:  {OUT_MANIFEST}")
    print(f"✅ Image-List:     {OUT_IMAGE_LIST}")

    # HTML Report
    print("Erzeuge Report ...")
    diversity_b64 = make_diversity_chart(coverage, STRATA_TARGETS)

    # Sample Cars: 5 pro Hauptklasse
    sample_cards_by_class = {}
    for cls in STRATA_TARGETS:
        candidates = cars_sorted[
            cars_sorted["stratified_selected"] &
            cars_sorted["plate_original"].apply(lambda p: cls in get_car_damage_types(p, damage_data))
        ].head(3)
        cards = []
        for _, row in candidates.iterrows():
            imgs = df[df["plate"] == row["plate_safe"]]["path"].tolist()
            cards.append(bf.render_car_card(row, imgs))
        sample_cards_by_class[cls] = cards

    sections_html = ""
    for cls, cards in sample_cards_by_class.items():
        if cards:
            sections_html += f'<div class="section"><h2>🏷 Beispiel-Autos mit <span class="cls">{cls}</span> ({coverage[cls]} Autos)</h2>{"".join(cards)}</div>'

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Stratified Car Selection Report</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f4f4f7; }}
.container {{ max-width: 1500px; margin: 0 auto; padding: 24px; }}
header.top {{ background: white; border-left: 5px solid #ff5f00; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 16px; }}
.stat {{ background: #f6f7f9; padding: 12px 16px; border-radius: 8px; }}
.stat .label {{ font-size: 11px; text-transform: uppercase; color: #5a6270; }}
.stat .value {{ font-size: 24px; font-weight: 700; }}
.section {{ background: white; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.car {{ background: #f9fafb; padding: 16px; border-radius: 10px; margin-bottom: 14px; }}
.car header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }}
.car h3 {{ margin: 0; font-family: monospace; color: #ff5f00; }}
.car .meta {{ display: flex; gap: 12px; font-size: 12px; flex-wrap: wrap; }}
.car .meta span {{ background: #fff; padding: 3px 10px; border-radius: 4px; border: 1px solid #e2e5ea; }}
.thumbs {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; }}
.thumb {{ position: relative; }}
.thumb img {{ width: 100%; border-radius: 4px; }}
.thumb span {{ position: absolute; bottom: 2px; left: 2px; background: rgba(0,0,0,.7); color: white; font-size: 9px; padding: 1px 5px; border-radius: 3px; font-family: monospace; }}
img.chart {{ max-width: 100%; }}
.cls {{ background: #fff3e0; padding: 2px 8px; border-radius: 4px; font-family: monospace; }}
</style></head><body>
<div class="container">
  <header class="top">
    <h1>🎯 Stratified Car Selection — {n_sel} Autos</h1>
    <p>2-Stufen-Filter: Top {KEEP_TOP_PERCENT}% Quality + Damage-Type-Stratifizierung (rare classes oversampled)</p>
    <div class="stats">
      <div class="stat"><div class="label">Total Pool</div><div class="value">{len(cars):,}</div></div>
      <div class="stat"><div class="label">Eligible (Stufe 1)</div><div class="value">{len(eligible):,}</div></div>
      <div class="stat"><div class="label">Final Selected</div><div class="value" style="color:#10b981">{n_sel}</div></div>
      <div class="stat"><div class="label">Bilder im Pool</div><div class="value">{len(selected_images):,}</div></div>
      <div class="stat"><div class="label">Saubere Bilder</div><div class="value">{(~selected_images["is_hard_fail"]).sum():,}</div></div>
    </div>
  </header>
  <div class="section">
    <h2>Damage-Type-Coverage</h2>
    <img class="chart" src="data:image/png;base64,{diversity_b64}"/>
  </div>
  {sections_html}
</div></body></html>
"""
    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"✅ Report:        {OUT_HTML}")
