"""ML-Backend für Label Studio — generischer VLM-Damage-Detector.

Liest MODEL_NAME aus Env, ruft Sixt LLM Gateway (OpenAI-kompatibel).
Output im Label-Studio-RectangleLabels-Format.
"""
import base64
import io
import json
import os
import re
import time
from io import BytesIO
from typing import List

import requests
from fastapi import FastAPI, Request
from openai import OpenAI
from PIL import Image

GATEWAY_URL = "https://llm.orange.sixt.com/v1"
API_KEY = os.environ["LLM_GW_API_KEY"]
MODEL_NAME = os.environ["MODEL_NAME"]
LABEL_NAME = os.environ.get("LABEL_NAME", MODEL_NAME)

client = OpenAI(base_url=GATEWAY_URL, api_key=API_KEY)
app = FastAPI(title=f"Damage Detection — {LABEL_NAME}")

# Cache: image_path -> prediction (vermeidet doppelte Calls bei Re-Open)
PRED_CACHE = {}

VIEWS_DESC = {
    "EXTERIOR_FRONT_STRAIGHT": "Front view (head-on)",
    "EXTERIOR_REAR_STRAIGHT":  "Rear view (head-on)",
    "DIAGONAL_FRONT_LEFT":     "Front-left diagonal",
    "DIAGONAL_FRONT_RIGHT":    "Front-right diagonal",
    "DIAGONAL_REAR_LEFT":      "Rear-left diagonal",
    "DIAGONAL_REAR_RIGHT":     "Rear-right diagonal",
    "TYRE_RIM_FRONT_LEFT":     "Front-left wheel close-up",
    "TYRE_RIM_FRONT_RIGHT":    "Front-right wheel close-up",
    "TYRE_RIM_REAR_LEFT":      "Rear-left wheel close-up",
    "TYRE_RIM_REAR_RIGHT":     "Rear-right wheel close-up",
}


def extract_view(image_url):
    for v in VIEWS_DESC:
        if v in image_url:
            return v
    return "unknown view"


def build_prompt(view):
    return f"""You're inspecting a rental car. The photo shows: {VIEWS_DESC.get(view, view)}.

Find ALL visible damages: scratch | stone_chip | dent | crack | missing | major | other.

For EACH damage output a tight bounding box in [ymin, xmin, ymax, xmax] coordinates normalized 0-1000.

Respond ONLY with JSON:
{{
  "damages": [
    {{"bbox_2d": [ymin, xmin, ymax, xmax], "label": "scratch", "confidence": 0.85, "reasoning": "..."}}
  ]
}}

If no damages visible, return {{"damages": []}}."""


def load_image_b64(image_url, ls_files_root="/label-studio/files"):
    """Lädt Bild aus Label-Studio-Files-Volume oder HTTP-URL."""
    if image_url.startswith("/data/local-files"):
        # Label-Studio local-files URL — extrahiere echten Pfad
        # Format: /data/local-files/?d=exterior_photos/...
        match = re.search(r"d=([^&]+)", image_url)
        if match:
            rel_path = match.group(1)
            full_path = os.path.join(ls_files_root, rel_path)
            with open(full_path, "rb") as f:
                img_bytes = f.read()
        else:
            raise ValueError(f"Cannot parse local-files URL: {image_url}")
    elif image_url.startswith("/exterior_photos") or image_url.startswith("/"):
        with open(image_url, "rb") as f:
            img_bytes = f.read()
    else:
        # HTTP-Download
        r = requests.get(image_url, timeout=30)
        r.raise_for_status()
        img_bytes = r.content

    img = Image.open(BytesIO(img_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > 1280:
        ratio = 1280 / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}", img.size


def call_vlm(image_url):
    if image_url in PRED_CACHE:
        return PRED_CACHE[image_url]

    view = extract_view(image_url)
    prompt = build_prompt(view)
    data_uri, _ = load_image_b64(image_url)

    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt},
        ]}],
        temperature=0.1,
        max_tokens=8192,
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or ""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        parsed = json.loads(match.group(0)) if match else {"damages": []}

    PRED_CACHE[image_url] = parsed
    return parsed


def to_ls_result(damages, from_name="label", to_name="image"):
    """Konvertiert [{bbox_2d, label, confidence}] zu Label-Studio-RectangleLabels-Format."""
    results = []
    for d in damages:
        bbox = d.get("bbox_2d")
        if not bbox or len(bbox) != 4:
            continue
        ymin, xmin, ymax, xmax = bbox
        # LS-Koordinaten in % (0-100)
        results.append({
            "from_name": from_name,
            "to_name": to_name,
            "type": "rectanglelabels",
            "value": {
                "x":      xmin / 10.0,
                "y":      ymin / 10.0,
                "width":  (xmax - xmin) / 10.0,
                "height": (ymax - ymin) / 10.0,
                "rectanglelabels": [d.get("label", "other")],
            },
            "score": float(d.get("confidence", 0.5)),
            "meta": {"text": [d.get("reasoning", "")[:200]]},
        })
    return results


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "label": LABEL_NAME}


@app.get("/")
def root():
    return {"model_version": LABEL_NAME, "type": "RectangleLabels"}


@app.post("/setup")
async def setup(req: Request):
    body = await req.json()
    return {"model_version": LABEL_NAME}


@app.post("/predict")
async def predict(req: Request):
    body = await req.json()
    tasks = body.get("tasks", [])
    results = []
    for task in tasks:
        try:
            image_url = task["data"].get("image") or task["data"].get("image_url")
            t0 = time.time()
            parsed = call_vlm(image_url)
            damages = parsed.get("damages", []) or parsed.get("visible_damages", [])
            ls_result = to_ls_result(damages)
            results.append({
                "result": ls_result,
                "score": float(max((d.get("confidence", 0) for d in damages), default=0)),
                "model_version": LABEL_NAME,
                "cluster": None,
                "meta": {"latency_s": round(time.time() - t0, 1), "n_damages": len(damages)},
            })
        except Exception as e:
            results.append({"result": [], "model_version": LABEL_NAME, "meta": {"error": str(e)[:200]}})
    return {"results": results, "model_version": LABEL_NAME}


@app.post("/webhook")
async def webhook(req: Request):
    # Label Studio kann Webhooks senden — wir ignorieren sie aktuell
    return {"status": "ok"}
