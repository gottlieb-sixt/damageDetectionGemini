"""Smoke Test mit 10 Autos aus den stratifizierten 500.
Gemini schaut FRISCH (kein DB-Hint), HTML zeigt: Auto-davor vs. detected damages.
"""
import base64
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from html import escape
from pathlib import Path

import pandas as pd
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# === Config ===
STRATIFIED = Path(__file__).parent / "stratified_cars.parquet"
IMAGE_LIST = Path(__file__).parent / "stratified_images.parquet"
DAMAGE_CASES = Path(__file__).parent.parent.parent / "damage_cases.json"
OUT_JSON = Path(__file__).parent / "smoke10_outputs.json"
OUT_HTML = Path(__file__).parent / "smoke10_report.html"

GATEWAY_BASE = "https://llm.orange.sixt.com/v1"
GATEWAY_KEY_ENV = "LLM_GW_API_KEY"
MODEL = "vertex_ai/gemini-3-pro"
N_PARALLEL = 6

VIEWS_DESC = {
    "EXTERIOR_FRONT_STRAIGHT": "Front view (head-on)",
    "EXTERIOR_REAR_STRAIGHT":  "Rear view (head-on)",
    "DIAGONAL_FRONT_LEFT":     "Front-left diagonal (3/4 from driver side front)",
    "DIAGONAL_FRONT_RIGHT":    "Front-right diagonal (3/4 from passenger side front)",
    "DIAGONAL_REAR_LEFT":      "Rear-left diagonal (3/4 from driver side rear)",
    "DIAGONAL_REAR_RIGHT":     "Rear-right diagonal (3/4 from passenger side rear)",
    "TYRE_RIM_FRONT_LEFT":     "Front-left wheel/rim close-up",
    "TYRE_RIM_FRONT_RIGHT":    "Front-right wheel/rim close-up",
    "TYRE_RIM_REAR_LEFT":      "Rear-left wheel/rim close-up",
    "TYRE_RIM_REAR_RIGHT":     "Rear-right wheel/rim close-up",
    "exterior_legacy":         "Exterior view (legacy format, angle unclear)",
}

COLORS = {
    "scratch": (255, 80, 80), "stone_chip": (255, 160, 0),
    "dent": (60, 180, 255), "crack": (200, 60, 255),
    "missing": (255, 60, 200), "major": (255, 20, 60),
    "other": (120, 120, 120),
}

TYPE_MAP = {
    "TYPE_SCRATCH": "scratch", "TYPE_STONE_CHIP": "stone_chip",
    "TYPE_STONE_CHIP_WITH_CRACK": "stone_chip", "TYPE_DENT": "dent",
    "TYPE_DENTED": "dent", "TYPE_CRACK": "crack", "TYPE_HOLE": "crack",
    "TYPE_BROKEN": "crack", "TYPE_MISSING": "missing", "TYPE_LOOSE": "missing",
    "TYPE_CRASH": "major", "TYPE_HAIL_DAMAGE": "major",
}


def safe(name): return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def select_10_cars(cars_df, damage_data):
    """Picke 10 Autos quer durch alle Damage-Klassen — die jeweils besten."""
    selected = cars_df[cars_df["stratified_selected"]].copy()
    selected["damage_classes"] = selected["plate_original"].apply(
        lambda p: {TYPE_MAP.get(d.get("type"), "other")
                   for c in damage_data.get(p, {}).get("damage_cases", [])
                   for d in c.get("damages", [])}
    )
    picks = []
    seen_plates = set()
    targets = [("major", 1), ("crack", 1), ("missing", 1), ("dent", 2),
               ("stone_chip", 2), ("scratch", 2), ("other", 1)]
    for cls, n in targets:
        cands = selected[
            (~selected["plate_safe"].isin(seen_plates)) &
            (selected["damage_classes"].apply(lambda s: cls in s))
        ].sort_values("car_score", ascending=False)
        for _, row in cands.head(n).iterrows():
            picks.append(row)
            seen_plates.add(row["plate_safe"])
    return pd.DataFrame(picks).head(10)


def build_prompt(view):
    view_desc = VIEWS_DESC.get(view, view)
    return f"""You're inspecting a rental car. The photo shows: **{view_desc}**.

Examine the photo carefully for ALL visible damages.
Common damages:
- scratch (lines/marks on paint, often diagonal)
- stone_chip (small impact craters, usually windscreen or hood)
- dent (deformation without paint loss usually)
- crack (broken glass, broken plastic)
- missing (missing part, broken-off piece)
- major (crash damage, heavy deformation, hail damage)
- other (graffiti, dirt mistaken as damage, etc.)

For EACH visible damage output:
1. bbox_2d [ymin, xmin, ymax, xmax] in 0-1000 normalized coordinates
2. label: one of scratch | stone_chip | dent | crack | missing | major | other
3. confidence: 0.0 - 1.0
4. severity: light | medium | severe
5. reasoning: brief description

Also assess:
- view_correct: does photo actually show the expected view? (bool)
- car_present: is there a clear vehicle? (bool)
- damages_visible_count: total visible damages (int)

ONLY respond with this JSON:
{{
  "view_correct": true,
  "car_present": true,
  "damages_visible_count": 2,
  "visible_damages": [
    {{
      "bbox_2d": [600, 200, 700, 350],
      "label": "scratch",
      "confidence": 0.85,
      "severity": "medium",
      "reasoning": "Diagonal scratch ~10cm on driver door"
    }}
  ],
  "photo_notes": "Lighting OK, slight shadow on left"
}}
"""


def parse_response(text):
    if not text: return {"error": "empty_response"}
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try: return json.loads(m.group(0))
            except: pass
        return {"error": f"parse_error: {e}"}


def encode_image(path, max_side=1280):
    img = Image.open(path)
    if img.mode != "RGB": img = img.convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}", img.size


def call_one(client, model, path, view):
    data_uri, size = encode_image(path)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": build_prompt(view)},
        ]}],
        temperature=0.1, max_tokens=8192,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content, size, resp.usage


def extract_view(filename):
    for v in VIEWS_DESC:
        if v in filename: return v
    return "UNKNOWN"


def process_image(client, path):
    view = extract_view(os.path.basename(path))
    try:
        text, size, usage = call_one(client, MODEL, path, view)
        return path, {
            "view": view,
            "image_size": size,
            "parsed": parse_response(text),
            "tokens_in": usage.prompt_tokens if usage else None,
            "tokens_out": usage.completion_tokens if usage else None,
        }
    except Exception as e:
        return path, {"view": view, "error": str(e)[:200]}


def draw_bboxes(image_path, damages, max_side=600):
    img = Image.open(image_path).convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")
    try: font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except: font = ImageFont.load_default()

    for i, d in enumerate(damages, 1):
        bbox = d.get("bbox_2d")
        if not bbox or len(bbox) != 4: continue
        ymin, xmin, ymax, xmax = bbox
        x1, y1 = xmin/1000*w, ymin/1000*h
        x2, y2 = xmax/1000*w, ymax/1000*h
        label = d.get("label", "?")
        color = COLORS.get(label, (200, 200, 0))
        for off in range(2):
            draw.rectangle([x1-off, y1-off, x2+off, y2+off], outline=color)
        text = f"{i}.{label} {d.get('confidence', 0):.0%}"
        tb = draw.textbbox((x1, max(0, y1-18)), text, font=font)
        draw.rectangle([tb[0]-2, tb[1]-2, tb[2]+4, tb[3]+2], fill=color+(220,))
        draw.text((x1, max(0, y1-18)), text, fill=(255,255,255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


def img_to_base64(path, max_side=600):
    img = Image.open(path).convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    return base64.b64encode(buf.getvalue()).decode()


def render_car_section(plate, car_meta, image_results, damage_data):
    """Pro Auto eine Sektion."""
    # DB-Damages
    db_dmgs = []
    for case in damage_data.get(plate, {}).get("damage_cases", []):
        for dmg in case.get("damages", []):
            loc = dmg.get("localized_values", {}) or {}
            db_dmgs.append({
                "type": dmg.get("type"),
                "master": TYPE_MAP.get(dmg.get("type"), "other"),
                "part": loc.get("part") or dmg.get("part"),
                "side": dmg.get("side"),
                "severity": loc.get("severity") or dmg.get("severity"),
            })

    # Pro View: Bilder ohne/mit Detections
    view_blocks = []
    total_detections = 0
    detection_summary = defaultdict(int)
    for path, r in image_results:
        view = r.get("view")
        parsed = r.get("parsed", {})
        if "error" in parsed:
            view_blocks.append(f'<div class="view-block err">Error: {escape(parsed["error"])}</div>')
            continue
        dmgs = parsed.get("visible_damages", [])
        total_detections += len(dmgs)
        for d in dmgs:
            detection_summary[d.get("label", "?")] += 1

        orig_b64 = img_to_base64(path)
        anno_b64 = draw_bboxes(path, dmgs)

        view_short = view.replace("EXTERIOR_", "").replace("DIAGONAL_", "DIAG_").replace("TYRE_RIM_", "TYR_") if view else "?"
        notes = parsed.get("photo_notes", "")

        det_list = ""
        if dmgs:
            for i, d in enumerate(dmgs, 1):
                clr = "#" + "".join(f"{c:02x}" for c in COLORS.get(d.get("label", ""), (120, 120, 120)))
                det_list += (
                    f"<li style='border-left-color:{clr}'>"
                    f"<strong>{escape(d.get('label', '?'))}</strong> "
                    f"({d.get('confidence', 0):.0%}, {escape(d.get('severity', '?'))})<br>"
                    f"<small>{escape(d.get('reasoning', ''))}</small></li>"
                )
        else:
            det_list = "<li class='nodet'>Keine Schäden detected</li>"

        view_blocks.append(f"""
<div class='view-block'>
  <h4>{view_short}</h4>
  <div class='img-pair'>
    <div><span class='caption'>VORHER</span><img src='data:image/jpeg;base64,{orig_b64}'/></div>
    <div><span class='caption'>GEMINI</span><img src='data:image/jpeg;base64,{anno_b64}'/></div>
  </div>
  <div class='notes'>📝 {escape(notes)}</div>
  <ol class='dets'>{det_list}</ol>
</div>""")

    # DB-Damages-Liste
    db_html = "<ol class='db-list'>"
    for d in db_dmgs[:30]:
        db_html += (
            f"<li><span class='cls {d['master']}'>{d['master']}</span> "
            f"<strong>{escape(str(d['part']))}</strong> · "
            f"{escape(str(d['side']))} · "
            f"<small>{escape(str(d['severity'])[:50])}</small></li>"
        )
    db_html += "</ol>"
    if len(db_dmgs) > 30:
        db_html += f"<small>+ {len(db_dmgs) - 30} weitere …</small>"

    det_summary_html = " · ".join(
        f"<span class='cls {k}'>{k}: {v}</span>"
        for k, v in sorted(detection_summary.items(), key=lambda x: -x[1])
    ) or "<span class='nodet'>nichts</span>"

    return f"""
<section class='car-section'>
  <header class='car-header'>
    <h2>{escape(plate)}</h2>
    <div class='stats'>
      <div><span>{car_meta['car_score']:.0f}</span><span>Score</span></div>
      <div><span>{car_meta['n_images']}</span><span>Bilder</span></div>
      <div><span>{len(db_dmgs)}</span><span>DB-damages</span></div>
      <div><span>{total_detections}</span><span>Gemini-detected</span></div>
    </div>
  </header>
  <div class='two-col'>
    <div>
      <h3>Was die DB sagt (alle Damages)</h3>
      {db_html}
    </div>
    <div>
      <h3>Gemini detection summary</h3>
      <div class='det-summary'>{det_summary_html}</div>
    </div>
  </div>
  <h3>Per-View: Original vs. Gemini-Detection</h3>
  <div class='views-grid'>{''.join(view_blocks)}</div>
</section>
"""


if __name__ == "__main__":
    api_key = os.environ.get(GATEWAY_KEY_ENV)
    if not api_key:
        print(f"❌ {GATEWAY_KEY_ENV} nicht gesetzt"); sys.exit(1)
    client = OpenAI(base_url=GATEWAY_BASE, api_key=api_key)

    print("Lade Daten ...")
    cars = pd.read_parquet(STRATIFIED)
    images = pd.read_parquet(IMAGE_LIST)
    with open(DAMAGE_CASES) as f:
        damage_data = json.load(f)

    print("Wähle 10 Autos quer durch Damage-Klassen ...")
    picks = select_10_cars(cars, damage_data)
    print(f"  Picked {len(picks)} cars:")
    for _, p in picks.iterrows():
        print(f"    {p['plate_original']:<20} score={p['car_score']:.0f} damages={p['n_damages']}")

    # Bilder pro Auto
    images_by_plate = defaultdict(list)
    for _, img in images.iterrows():
        if img["plate"] in set(picks["plate_safe"]):
            images_by_plate[img["plate"]].append(img["path"])

    total_imgs = sum(len(v) for v in images_by_plate.values())
    print(f"\nGesamt: {total_imgs} Bilder → Gemini-Pass (parallel {N_PARALLEL}) ...")

    # Resume
    results = {}
    if OUT_JSON.exists():
        with open(OUT_JSON) as f: results = json.load(f)
        print(f"  Resume: {len(results)} bereits fertig.")

    todo = []
    for plate, paths in images_by_plate.items():
        for p in paths:
            if p not in results:
                todo.append(p)
    print(f"  ToDo: {len(todo)} Bilder")

    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=N_PARALLEL) as ex:
        futures = {ex.submit(process_image, client, p): p for p in todo}
        for fut in as_completed(futures):
            path, r = fut.result()
            results[path] = r
            completed += 1
            elapsed = time.time() - t0
            n_d = len(r.get("parsed", {}).get("visible_damages", [])) if "parsed" in r else 0
            err = "✗" if "error" in r or "error" in r.get("parsed", {}) else "✓"
            print(f"  [{completed:>3}/{len(todo)}] {err} {n_d} dmgs · {os.path.basename(path)[:50]} ({elapsed:.0f}s)", flush=True)
            if completed % 10 == 0:
                with open(OUT_JSON, "w") as f: json.dump(results, f, indent=2)

    with open(OUT_JSON, "w") as f: json.dump(results, f, indent=2)
    print(f"\n✅ Gemini-Pass: {len(results)} Bilder in {time.time()-t0:.0f}s")

    # === HTML Report ===
    print("Erzeuge HTML ...")
    sections = []
    for _, car in picks.iterrows():
        plate_safe = car["plate_safe"]
        plate_orig = car["plate_original"]
        img_results = [(p, results[p]) for p in images_by_plate[plate_safe] if p in results]
        sections.append(render_car_section(plate_orig, car, img_results, damage_data))

    # Global Stats
    total_dets = sum(len(r.get("parsed", {}).get("visible_damages", [])) for r in results.values()
                     if isinstance(r.get("parsed", {}).get("visible_damages"), list))
    total_in = sum(r.get("tokens_in") or 0 for r in results.values())
    total_out = sum(r.get("tokens_out") or 0 for r in results.values())

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Smoke Test 10 Autos — Gemini Detection Validation</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f4f4f7; }}
.container {{ max-width: 1600px; margin: 0 auto; padding: 24px; }}
header.top {{ background: white; border-left: 5px solid #ff5f00; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-top: 12px; }}
.stat {{ background: #f6f7f9; padding: 10px 14px; border-radius: 8px; }}
.stat .label {{ font-size: 11px; text-transform: uppercase; color: #5a6270; }}
.stat .value {{ font-size: 22px; font-weight: 700; }}
.car-section {{ background: white; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.car-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #e2e5ea; padding-bottom: 12px; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }}
.car-header h2 {{ margin: 0; color: #ff5f00; font-family: monospace; }}
.car-header .stats {{ display: flex; gap: 16px; margin: 0; }}
.car-header .stats > div {{ display: flex; flex-direction: column; align-items: flex-end; }}
.car-header .stats span:first-child {{ font-size: 20px; font-weight: 700; }}
.car-header .stats span:last-child {{ font-size: 10px; text-transform: uppercase; color: #5a6270; }}
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 20px; }}
@media (max-width: 900px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
.db-list {{ list-style: none; padding: 0; max-height: 280px; overflow-y: auto; font-size: 13px; }}
.db-list li {{ padding: 6px 10px; border-bottom: 1px solid #e2e5ea; }}
.cls {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-right: 6px; }}
.cls.scratch {{ background: #fee2e2; color: #dc2626; }}
.cls.stone_chip {{ background: #ffedd5; color: #c2410c; }}
.cls.dent {{ background: #dbeafe; color: #1d4ed8; }}
.cls.crack {{ background: #f3e8ff; color: #7e22ce; }}
.cls.missing {{ background: #fce7f3; color: #be185d; }}
.cls.major {{ background: #fee2e2; color: #991b1b; }}
.cls.other {{ background: #e2e5ea; color: #4b5563; }}
.det-summary {{ background: #f6f7f9; padding: 16px; border-radius: 8px; font-size: 14px; line-height: 2; }}
.views-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(560px, 1fr)); gap: 18px; }}
.view-block {{ background: #f9fafb; padding: 14px; border-radius: 10px; }}
.view-block h4 {{ margin: 0 0 10px; font-family: monospace; color: #1a1d23; font-size: 13px; }}
.view-block.err {{ background: #fee2e2; color: #dc2626; padding: 20px; text-align: center; }}
.img-pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }}
.img-pair > div {{ position: relative; }}
.img-pair img {{ width: 100%; border-radius: 4px; }}
.img-pair .caption {{ position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.7); color: white; font-size: 10px; padding: 2px 6px; border-radius: 3px; font-weight: 600; letter-spacing: .5px; }}
.notes {{ font-size: 11px; padding: 6px 10px; background: #fff7e6; border-left: 3px solid #d97706; border-radius: 4px; margin: 8px 0; }}
.dets {{ list-style: none; padding: 0; font-size: 12px; }}
.dets li {{ padding: 6px 10px; background: white; border-left: 3px solid #ccc; margin-bottom: 4px; border-radius: 0 4px 4px 0; }}
.dets .nodet {{ font-style: italic; color: #5a6270; background: transparent; border: none; }}
.nodet {{ font-style: italic; color: #5a6270; }}
</style></head><body>
<div class="container">
  <header class="top">
    <h1>🔍 Smoke Test 10 Autos — Gemini Free Detection (kein DB-Hint)</h1>
    <p>Validierung: detected Gemini Damages auf den qualitäts-gefilterten Bildern?</p>
    <div class="stats">
      <div class="stat"><div class="label">Autos</div><div class="value">{len(picks)}</div></div>
      <div class="stat"><div class="label">Bilder analysiert</div><div class="value">{len(results)}</div></div>
      <div class="stat"><div class="label">Total Detections</div><div class="value">{total_dets}</div></div>
      <div class="stat"><div class="label">Ø Detections/Auto</div><div class="value">{total_dets/len(picks):.1f}</div></div>
      <div class="stat"><div class="label">Tokens in</div><div class="value">{total_in:,}</div></div>
      <div class="stat"><div class="label">Tokens out</div><div class="value">{total_out:,}</div></div>
    </div>
  </header>
  {''.join(sections)}
</div></body></html>
"""
    with open(OUT_HTML, "w") as f: f.write(html)
    print(f"✅ HTML: {OUT_HTML}")
