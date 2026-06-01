"""Batch-run alle 3 AI-Modelle auf den 24 Test-Bildern + HTML-Report.

Output:
  test_predictions_report.html (mit allen Bildern + Modell-Output)
"""
import base64
import io
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

API_BASE = "http://localhost:8000"
DB = Path(__file__).parent / "data" / "annotations.db"
OUT_HTML = Path(__file__).parent.parent / "test_predictions_report.html"
N_PARALLEL = 6

MODELS = ["gemini", "openai", "claude"]
MODEL_INFO = {
    "gemini":  {"name": "Gemini 3.1 Pro",  "color": "#ef4444", "id_match": "gemini"},
    "openai":  {"name": "GPT-5.5",         "color": "#3b82f6", "id_match": "gpt"},
    "claude":  {"name": "Claude Opus 4.7", "color": "#10b981", "id_match": "claude"},
}


def get_test_images():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT i.id, i.path, i.view, c.plate_original, c.plate_safe
        FROM images i JOIN cars c ON i.plate_safe = c.plate_safe
        WHERE COALESCE(c.is_test, 0) = 1
        ORDER BY c.plate_safe, i.view
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run_one(image_id: int, model_key: str):
    """Ruft API → triggert Modell, gibt zurück was die DB hat."""
    try:
        r = requests.get(f"{API_BASE}/api/images/{image_id}/predictions",
                         params={"model": model_key}, timeout=180)
        r.raise_for_status()
        data = r.json()
        # data ist {model_id: parsed}, finde das richtige
        for mid, parsed in data.items():
            if MODEL_INFO[model_key]["id_match"] in mid:
                return image_id, model_key, parsed
        return image_id, model_key, {"error": "no_match"}
    except Exception as e:
        return image_id, model_key, {"error": str(e)[:200]}


def encode_with_bboxes(image_path, damages_per_model, max_side=900):
    """Rendert ein Bild mit BBoxes aller 3 Modelle übereinander."""
    img = Image.open(image_path).convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    W, H = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except Exception:
        font = ImageFont.load_default()

    for model_key, damages in damages_per_model.items():
        info = MODEL_INFO[model_key]
        rgb = tuple(int(info["color"][i:i+2], 16) for i in (1, 3, 5))
        for i, d in enumerate(damages, 1):
            bbox = d.get("bbox_2d")
            if not bbox or len(bbox) != 4:
                continue
            ymin, xmin, ymax, xmax = bbox
            x1, y1 = xmin/1000*W, ymin/1000*H
            x2, y2 = xmax/1000*W, ymax/1000*H
            for off in range(2):
                draw.rectangle([x1-off, y1-off, x2+off, y2+off], outline=rgb)
            text = f"{info['name'][:1]}{i}.{d.get('label','?')}"
            tb = draw.textbbox((x1, max(0, y1-18)), text, font=font)
            draw.rectangle([tb[0]-2, tb[1]-2, tb[2]+4, tb[3]+2], fill=rgb+(220,))
            draw.text((x1, max(0, y1-18)), text, fill=(255,255,255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def img_to_b64(path, max_side=900):
    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def get_damages(parsed):
    if not isinstance(parsed, dict):
        return []
    return parsed.get("damages") or parsed.get("visible_damages") or []


def render_image_block(img_meta, results_for_image):
    """Pro Bild: Original + Combined-Overlay + 3 Modell-Output-Listen."""
    damages_per_model = {m: get_damages(results_for_image.get(m, {})) for m in MODELS}
    total_dets = sum(len(d) for d in damages_per_model.values())

    orig_b64 = img_to_b64(img_meta["path"])
    combined_b64 = encode_with_bboxes(img_meta["path"], damages_per_model)

    # Pro-Modell-Listen
    blocks_html = ""
    for m in MODELS:
        info = MODEL_INFO[m]
        damages = damages_per_model[m]
        err = results_for_image.get(m, {}).get("error")
        if err:
            blocks_html += f'<div class="model-block" style="border-color:{info["color"]}"><h4 style="color:{info["color"]}">{escape(info["name"])}</h4><div class="err">{escape(err)}</div></div>'
            continue
        dets_html = ""
        if damages:
            for i, d in enumerate(damages, 1):
                dets_html += (
                    f'<li><strong>{i}.{escape(d.get("label","?"))}</strong> '
                    f'<span class="conf">{int((d.get("confidence",0))*100)}%</span> '
                    f'<span class="sev">{escape(d.get("severity",""))}</span>'
                    f'<div class="reasoning">{escape(d.get("reasoning","")[:200])}</div></li>'
                )
        else:
            dets_html = '<li class="nodet">keine Detection</li>'
        blocks_html += (
            f'<div class="model-block" style="border-color:{info["color"]}">'
            f'<h4 style="color:{info["color"]}">{escape(info["name"])} '
            f'<span class="count">({len(damages)})</span></h4>'
            f'<ul>{dets_html}</ul></div>'
        )

    view_label = img_meta["view"]
    return f"""
<div class="img-section">
  <div class="img-header">
    <h3>{escape(view_label)} <span class="filename">({escape(Path(img_meta["path"]).name)})</span></h3>
    <span class="total">{total_dets} total detections</span>
  </div>
  <div class="img-grid">
    <div>
      <div class="caption">ORIGINAL</div>
      <img src="data:image/jpeg;base64,{orig_b64}"/>
    </div>
    <div>
      <div class="caption">ALLE MODELLE</div>
      <img src="data:image/jpeg;base64,{combined_b64}"/>
    </div>
  </div>
  <div class="model-blocks">{blocks_html}</div>
</div>
"""


def render_car_section(plate_original, images, all_results):
    image_blocks = ""
    for img_meta in images:
        results_for_image = all_results.get(img_meta["id"], {})
        image_blocks += render_image_block(img_meta, results_for_image)
    return f'<section class="car-section"><h2>{escape(plate_original)} ({len(images)} Bilder)</h2>{image_blocks}</section>'


def main():
    print(f"Lade Test-Bilder ...")
    images = get_test_images()
    print(f"  {len(images)} Bilder, {len(set(i['plate_safe'] for i in images))} Autos")

    print(f"\nStarte Predictions ({len(images)*len(MODELS)} calls, {N_PARALLEL} parallel) ...")
    jobs = [(img["id"], m) for img in images for m in MODELS]
    all_results = {}  # image_id -> {model_key: parsed}

    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=N_PARALLEL) as ex:
        futures = [ex.submit(run_one, iid, m) for iid, m in jobs]
        for f in as_completed(futures):
            image_id, model_key, parsed = f.result()
            all_results.setdefault(image_id, {})[model_key] = parsed
            completed += 1
            dmgs = get_damages(parsed)
            err = parsed.get("error") if isinstance(parsed, dict) else None
            status = "✗ "+err[:40] if err else f"✓ {len(dmgs)} dmgs"
            print(f"  [{completed:>2}/{len(jobs)}] img={image_id} {model_key:<7} {status}", flush=True)
    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.0f}s")

    # HTML
    print(f"\nGeneriere HTML-Report ...")
    by_plate = {}
    for img in images:
        by_plate.setdefault(img["plate_original"], []).append(img)

    sections = ""
    for plate, imgs in by_plate.items():
        sections += render_car_section(plate, imgs, all_results)

    # Stats
    n_dets_per_model = {m: 0 for m in MODELS}
    n_imgs_with_det = {m: 0 for m in MODELS}
    for img_id, results in all_results.items():
        for m in MODELS:
            dmgs = get_damages(results.get(m, {}))
            n_dets_per_model[m] += len(dmgs)
            if dmgs:
                n_imgs_with_det[m] += 1

    stats_cards = ""
    for m in MODELS:
        info = MODEL_INFO[m]
        stats_cards += (
            f'<div class="stat-card" style="border-color:{info["color"]}">'
            f'<div class="stat-name" style="color:{info["color"]}">{info["name"]}</div>'
            f'<div class="stat-val">{n_dets_per_model[m]} <small>detections</small></div>'
            f'<div class="stat-sub">{n_imgs_with_det[m]}/{len(images)} Bilder mit Detection</div>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<title>Test Predictions Report — 3 Modelle vs. 24 Test-Bilder</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f4f4f7; color: #1a1d23; }}
.container {{ max-width: 1600px; margin: 0 auto; padding: 24px; }}
header.top {{ background: white; border-left: 5px solid #ff5f00; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,.06); }}
header.top h1 {{ margin: 0 0 8px; }}
.stats-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 16px; }}
.stat-card {{ background: #f9fafb; padding: 14px 18px; border-radius: 8px; border-left: 4px solid; }}
.stat-name {{ font-size: 13px; text-transform: uppercase; font-weight: 700; }}
.stat-val {{ font-size: 28px; font-weight: 700; margin-top: 4px; }}
.stat-val small {{ font-size: 13px; color: #5a6270; font-weight: 400; }}
.stat-sub {{ font-size: 12px; color: #5a6270; margin-top: 4px; }}
.car-section {{ background: white; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.car-section h2 {{ margin: 0 0 20px; color: #ff5f00; font-family: monospace; border-bottom: 2px solid #e2e5ea; padding-bottom: 8px; }}
.img-section {{ margin-bottom: 32px; padding-bottom: 24px; border-bottom: 1px dashed #e2e5ea; }}
.img-section:last-child {{ border-bottom: none; }}
.img-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }}
.img-header h3 {{ margin: 0; font-family: monospace; font-size: 16px; }}
.filename {{ color: #9ca3af; font-size: 12px; font-weight: 400; }}
.total {{ font-weight: 700; color: #ff5f00; font-size: 14px; }}
.img-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }}
.img-grid > div {{ position: relative; }}
.img-grid img {{ width: 100%; border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,.1); }}
.caption {{ position: absolute; top: 8px; left: 8px; background: rgba(0,0,0,.75); color: white; font-size: 11px; padding: 3px 8px; border-radius: 4px; font-weight: 600; letter-spacing: .5px; z-index: 1; }}
.model-blocks {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.model-block {{ background: #f9fafb; padding: 12px; border-radius: 8px; border-left: 4px solid; }}
.model-block h4 {{ margin: 0 0 8px; font-size: 13px; }}
.model-block .count {{ color: #6b7280; font-weight: 400; }}
.model-block ul {{ list-style: none; padding: 0; margin: 0; font-size: 12px; }}
.model-block li {{ padding: 6px 8px; background: white; border-radius: 4px; margin-bottom: 4px; }}
.model-block .conf {{ color: #6b7280; }}
.model-block .sev {{ color: #6b7280; font-style: italic; }}
.model-block .reasoning {{ color: #4b5563; font-size: 11px; margin-top: 3px; line-height: 1.4; }}
.model-block .nodet {{ font-style: italic; color: #9ca3af; }}
.model-block .err {{ color: #dc2626; font-size: 12px; padding: 8px; background: #fee2e2; border-radius: 4px; }}
@media (max-width: 1100px) {{ .img-grid {{ grid-template-columns: 1fr; }} .model-blocks {{ grid-template-columns: 1fr; }} }}
</style></head><body>
<div class="container">
  <header class="top">
    <h1>🧪 Test Predictions Report</h1>
    <p>3 Modelle (Gemini 3.1 Pro · GPT-5.5 · Claude Opus 4.7) auf {len(images)} Test-Bildern aus {len(by_plate)} eigenen Autos</p>
    <div class="stats-grid">{stats_cards}</div>
  </header>
  {sections}
</div></body></html>"""

    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"✅ Report: {OUT_HTML}")
    print(f"\nGesamt-Detections:")
    for m in MODELS:
        print(f"  {MODEL_INFO[m]['name']:<22} {n_dets_per_model[m]} dets ({n_imgs_with_det[m]}/{len(images)} Bilder)")


if __name__ == "__main__":
    main()
