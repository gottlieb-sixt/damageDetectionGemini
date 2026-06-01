"""Custom Annotation Tool — FastAPI Backend.

Endpoints:
- Cars: list, detail, images
- Annotations: CRUD
- Predictions: Gemini 3.1 Pro + GPT-5.5 via Sixt LLM Gateway
- Export: COCO + YOLO
"""
import base64
import io
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import cv2
import numpy as np
import torch
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image
from pydantic import BaseModel

# .env aus dem annotation_tool/ Root laden
load_dotenv(Path(__file__).parent.parent / ".env")

# === Paths ===
ROOT = Path(__file__).parent.parent.parent.parent  # get_anglepicture_aftercleaning/
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "annotations.db"
DAMAGE_CASES = ROOT / "damage_cases.json"
PHOTOS_EXPORT = ROOT / "photos_export.json"
STRATIFIED_CARS = ROOT / "pipeline" / "quality" / "stratified_cars.parquet"
STRATIFIED_IMAGES = ROOT / "pipeline" / "quality" / "stratified_images.parquet"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# Damage-Filter-Konfiguration
EXCLUDED_DAMAGE_GROUPS = {"WINDOWS", "TYRES"}
DAMAGE_FILTER_ENABLED = True  # nur Damages vor Foto-Zeitpunkt + ohne Scheibe/Felge

# Lazy-loaded: plate → max(completed_at) Unix-Timestamp
_plate_photo_ts: dict = {}


def get_plate_photo_ts():
    """Lazy-Load Foto-Timestamps pro Plate aus photos_export.json."""
    global _plate_photo_ts
    if _plate_photo_ts:
        return _plate_photo_ts
    if not PHOTOS_EXPORT.exists():
        return {}
    from datetime import datetime
    with open(PHOTOS_EXPORT) as f:
        data = json.load(f)
    for v in data:
        plate = v.get("license_plate", "")
        max_ts = 0
        for t in v.get("tasks", []):
            ca = t.get("completed_at")
            if not ca: continue
            try:
                ts = datetime.strptime(ca, "%Y-%m-%dT%H:%M:%S").timestamp()
                max_ts = max(max_ts, ts)
            except Exception:
                pass
        if max_ts > 0:
            _plate_photo_ts[plate] = max_ts
    return _plate_photo_ts

# === LLM Gateway ===
GATEWAY_URL = "https://llm.orange.sixt.com/v1"
GATEWAY_KEY = os.environ.get("LLM_GW_API_KEY", "")
MODELS = {
    "gemini": "vertex_ai/gemini-3.1-pro",
    "flash":  "vertex_ai/gemini-3.5-flash",
}
llm = OpenAI(base_url=GATEWAY_URL, api_key=GATEWAY_KEY) if GATEWAY_KEY else None

# === SAM (lazy-loaded) ===
SAM_DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
_sam_model = None
_sam_processor = None


def get_sam():
    """Lazy-load SAM v1 base (~375MB DL beim ersten Mal)."""
    global _sam_model, _sam_processor
    if _sam_model is None:
        from transformers import SamModel, SamProcessor
        print(f"[SAM] Lade facebook/sam-vit-base auf {SAM_DEVICE} ...")
        _sam_processor = SamProcessor.from_pretrained("facebook/sam-vit-base")
        _sam_model = SamModel.from_pretrained("facebook/sam-vit-base").to(SAM_DEVICE).eval()
        print(f"[SAM] Ready")
    return _sam_model, _sam_processor


def sam_predict(image_path: str, *, bbox_norm: tuple | None = None,
                point_norm: tuple | None = None) -> list | None:
    """SAM mit Box ODER Point Prompt.
    bbox_norm = (x, y, w, h) | point_norm = (x, y) — beide 0-1 normalisiert.
    Output: Polygon [[x,y], ...] normalisiert.
    """
    model, processor = get_sam()
    img = Image.open(image_path).convert("RGB")
    W, H = img.size

    kwargs = {"images": img, "return_tensors": "pt"}
    if bbox_norm is not None:
        x, y, w, h = bbox_norm
        kwargs["input_boxes"] = [[[x*W, y*H, (x+w)*W, (y+h)*H]]]
    elif point_norm is not None:
        px, py = point_norm
        kwargs["input_points"] = [[[px*W, py*H]]]
        kwargs["input_labels"] = [[1]]  # 1 = Vordergrund
    else:
        raise ValueError("Brauche bbox_norm oder point_norm")

    inputs = processor(**kwargs)
    # MPS unterstützt kein float64 — alle Float-Tensoren auf float32 casten
    for k, v in inputs.items():
        if torch.is_tensor(v) and v.dtype == torch.float64:
            inputs[k] = v.to(torch.float32)
    inputs = inputs.to(SAM_DEVICE)
    with torch.no_grad():
        out = model(**inputs)
    masks = processor.image_processor.post_process_masks(
        out.pred_masks.cpu(),
        inputs["original_sizes"].cpu(),
        inputs["reshaped_input_sizes"].cpu(),
    )
    # masks[0] shape: [1, num_masks, H, W]. Pick highest-scored mask.
    mask_np = masks[0][0][0].numpy().astype(np.uint8) * 255

    contours, _ = cv2.findContours(mask_np, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    # Simplify (Douglas-Peucker)
    epsilon = 0.003 * cv2.arcLength(contour, True)
    poly = cv2.approxPolyDP(contour, epsilon, True)
    return [[float(p[0][0] / W), float(p[0][1] / H)] for p in poly]

# === Damage Classes ===
DAMAGE_CLASSES = ["scratch", "stone_chip", "dent", "crack", "missing", "major", "other"]
TYPE_MAP = {
    "TYPE_SCRATCH": "scratch", "TYPE_STONE_CHIP": "stone_chip",
    "TYPE_STONE_CHIP_WITH_CRACK": "stone_chip", "TYPE_DENT": "dent",
    "TYPE_DENTED": "dent", "TYPE_CRACK": "crack", "TYPE_HOLE": "crack",
    "TYPE_BROKEN": "crack", "TYPE_MISSING": "missing", "TYPE_LOOSE": "missing",
    "TYPE_CRASH": "major", "TYPE_HAIL_DAMAGE": "major",
}


# === DB Setup ===
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Schema + Daten-Initial-Load (Cars, Images, DB-Damages)."""
    import pandas as pd

    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS cars (
            plate_safe TEXT PRIMARY KEY,
            plate_original TEXT,
            car_score REAL,
            n_unique_views INTEGER,
            n_damages INTEGER,
            damage_classes TEXT  -- comma-separated master classes
        );
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_safe TEXT,
            view TEXT,
            path TEXT UNIQUE,
            width INTEGER,
            height INTEGER,
            quality_score REAL,
            is_hard_fail INTEGER,
            FOREIGN KEY (plate_safe) REFERENCES cars(plate_safe)
        );
        CREATE INDEX IF NOT EXISTS idx_images_plate ON images(plate_safe);

        CREATE TABLE IF NOT EXISTS db_damages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_safe TEXT,
            damage_id TEXT,
            type TEXT,
            master_class TEXT,
            part TEXT,
            side TEXT,
            severity TEXT,
            projection TEXT,
            segment TEXT,
            FOREIGN KEY (plate_safe) REFERENCES cars(plate_safe)
        );

        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            -- bbox normalisiert 0-1
            x REAL, y REAL, w REAL, h REAL,
            label TEXT NOT NULL,
            severity TEXT,             -- light/medium/severe
            paint_damaged INTEGER,     -- 0/1
            matched_db_damage_id TEXT, -- optional Link zu db_damages
            annotator TEXT DEFAULT 'default',
            source TEXT DEFAULT 'human',  -- human/gemini/openai
            created_at REAL,
            updated_at REAL,
            FOREIGN KEY (image_id) REFERENCES images(id)
        );
        CREATE INDEX IF NOT EXISTS idx_anno_image ON annotations(image_id);
        CREATE INDEX IF NOT EXISTS idx_anno_source ON annotations(source);

        CREATE TABLE IF NOT EXISTS sam_polygons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL,
            polygon_json TEXT,
            source_model TEXT,
            created_at REAL,
            UNIQUE(image_id, bbox_x, bbox_y, bbox_w, bbox_h),
            FOREIGN KEY (image_id) REFERENCES images(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sam_image ON sam_polygons(image_id);

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            model TEXT NOT NULL,  -- gemini-3.1-pro / gpt-5.5
            raw_json TEXT,
            n_damages INTEGER,
            tokens_in INTEGER,
            tokens_out INTEGER,
            latency_s REAL,
            created_at REAL,
            UNIQUE(image_id, model),
            FOREIGN KEY (image_id) REFERENCES images(id)
        );
        """)
        conn.commit()

        # Initial-Load wenn leer
        cur = conn.execute("SELECT COUNT(*) FROM cars")
        if cur.fetchone()[0] > 0:
            return

        print("Init DB: Lade stratifizierte Daten ...")
        cars_df = pd.read_parquet(STRATIFIED_CARS)
        cars_df = cars_df[cars_df["stratified_selected"]]
        imgs_df = pd.read_parquet(STRATIFIED_IMAGES)
        with open(DAMAGE_CASES) as f:
            damage_data = json.load(f)

        # Cars
        for _, row in cars_df.iterrows():
            plate_orig = row["plate_original"]
            classes = set()
            for case in damage_data.get(plate_orig, {}).get("damage_cases", []):
                for d in case.get("damages", []):
                    classes.add(TYPE_MAP.get(d.get("type"), "other"))
            conn.execute(
                "INSERT INTO cars VALUES (?, ?, ?, ?, ?, ?)",
                (row["plate_safe"], plate_orig, float(row["car_score"]),
                 int(row["n_unique_views"]), int(row["n_damages"]),
                 ",".join(sorted(classes)))
            )

        # Images
        for _, row in imgs_df.iterrows():
            try:
                with Image.open(row["path"]) as img:
                    w, h = img.size
            except Exception:
                w = h = 0
            conn.execute(
                "INSERT INTO images (plate_safe, view, path, width, height, quality_score, is_hard_fail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["plate"], row["view"], row["path"], w, h,
                 float(row["quality_score"]), int(row["is_hard_fail"]))
            )

        # DB-Damages
        plate_to_safe = {p_orig: row["plate_safe"]
                         for _, row in cars_df.iterrows() for p_orig in [row["plate_original"]]}
        for plate_orig, plate_safe in plate_to_safe.items():
            for case in damage_data.get(plate_orig, {}).get("damage_cases", []):
                for dmg in case.get("damages", []):
                    loc = dmg.get("localized_values", {}) or {}
                    for coord in dmg.get("coordinates", []) or [{}]:
                        conn.execute(
                            "INSERT INTO db_damages (plate_safe, damage_id, type, master_class, part, side, severity, projection, segment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (plate_safe, dmg.get("damage_id", ""),
                             dmg.get("type"), TYPE_MAP.get(dmg.get("type"), "other"),
                             loc.get("part") or dmg.get("part"),
                             dmg.get("side"),
                             loc.get("severity") or dmg.get("severity"),
                             coord.get("projection"), coord.get("segment"))
                        )

        conn.commit()
        print(f"  Cars: {conn.execute('SELECT COUNT(*) FROM cars').fetchone()[0]}")
        print(f"  Images: {conn.execute('SELECT COUNT(*) FROM images').fetchone()[0]}")
        print(f"  DB-Damages: {conn.execute('SELECT COUNT(*) FROM db_damages').fetchone()[0]}")


# === Pydantic Models ===
class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class AnnotationIn(BaseModel):
    image_id: int
    x: float
    y: float
    w: float
    h: float
    label: str
    severity: Optional[str] = None
    paint_damaged: Optional[bool] = None
    matched_db_damage_id: Optional[str] = None
    annotator: str = "default"
    source: str = "human"


class AnnotationUpdate(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    w: Optional[float] = None
    h: Optional[float] = None
    label: Optional[str] = None
    severity: Optional[str] = None
    paint_damaged: Optional[bool] = None


# === App ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Damage Annotation Tool", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# === Cars ===
@app.get("/api/cars")
def list_cars(limit: int = 100, offset: int = 0, has_damages: bool = True, only_test: bool = False):
    with get_db() as conn:
        q = """
        SELECT c.plate_safe, c.plate_original, c.car_score, c.n_unique_views,
               c.n_damages, c.damage_classes,
               COALESCE(c.is_test, 0) AS is_test,
               (SELECT COUNT(*) FROM images i WHERE i.plate_safe = c.plate_safe) AS n_images,
               (SELECT COUNT(*) FROM annotations a JOIN images i ON a.image_id = i.id WHERE i.plate_safe = c.plate_safe AND a.source = 'human') AS n_human_annos
        FROM cars c
        """
        conds = []
        if only_test:
            conds.append("COALESCE(c.is_test, 0) = 1")
        elif has_damages:
            # Test-Cars haben 0 DB-Damages — wir wollen sie aber trotzdem sehen wenn has_damages an ist
            conds.append("(c.n_damages > 0 OR COALESCE(c.is_test, 0) = 1)")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        # Test-Cars zuerst, dann nach Score
        q += " ORDER BY COALESCE(c.is_test, 0) DESC, c.car_score DESC LIMIT ? OFFSET ?"
        rows = conn.execute(q, (limit, offset)).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/cars/{plate_safe}")
def get_car(plate_safe: str):
    with get_db() as conn:
        car = conn.execute("SELECT * FROM cars WHERE plate_safe = ?", (plate_safe,)).fetchone()
        if not car:
            raise HTTPException(404, "Car not found")
        images = [dict(r) for r in conn.execute(
            "SELECT id, view, path, width, height, quality_score, is_hard_fail FROM images WHERE plate_safe = ? ORDER BY view",
            (plate_safe,)
        ).fetchall()]
        damages = [dict(r) for r in conn.execute(
            "SELECT * FROM db_damages WHERE plate_safe = ?", (plate_safe,)
        ).fetchall()]
        for img in images:
            img["n_annotations"] = conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE image_id = ? AND source = 'human'", (img["id"],)
            ).fetchone()[0]
            extended = list(MODELS.values()) + [f"{m}#tiled" for m in MODELS.values()]
            img["has_predictions"] = {
                m: bool(conn.execute("SELECT 1 FROM predictions WHERE image_id = ? AND model = ?", (img["id"], m)).fetchone())
                for m in extended
            }

        # Damage-Cases mit Damages + Photos zusammen aus damage_cases.json
        # FILTERS:
        # 1. Exclude WINDOWS + TYRES groups (Scheiben/Felgen)
        # 2. Only damages registered BEFORE the photo was taken
        damage_cases_full = []
        n_filtered_out = {"by_group": 0, "by_time": 0, "kept": 0}
        try:
            with open(DAMAGE_CASES) as f:
                all_dc = json.load(f)
            plate_orig = car["plate_original"]
            plate_payload = all_dc.get(plate_orig, {})

            photo_ts_map = get_plate_photo_ts()
            photo_ts = photo_ts_map.get(plate_orig, 0)

            cases_meta = {}
            for case in plate_payload.get("damage_cases", []):
                cid = case.get("damage_case_id", "")
                case_ts = int(case.get("damage_created_at", {}).get("seconds", 0))

                # Zeit-Filter: nur Cases die VOR Foto-Zeitpunkt registriert wurden
                if DAMAGE_FILTER_ENABLED and photo_ts > 0 and case_ts > 0 and case_ts >= photo_ts:
                    n_filtered_out["by_time"] += len(case.get("damages", []))
                    continue

                dmgs = []
                seen_ids = set()
                for dmg in case.get("damages", []):
                    did = dmg.get("damage_id", "")
                    if did in seen_ids: continue
                    seen_ids.add(did)

                    # Group-Filter: Scheiben/Felgen raus
                    if DAMAGE_FILTER_ENABLED and dmg.get("group") in EXCLUDED_DAMAGE_GROUPS:
                        n_filtered_out["by_group"] += 1
                        continue

                    loc = dmg.get("localized_values", {}) or {}
                    dmgs.append({
                        "damage_id": did,
                        "type": dmg.get("type"),
                        "master_class": TYPE_MAP.get(dmg.get("type"), "other"),
                        "part": loc.get("part") or dmg.get("part") or "?",
                        "side": dmg.get("side"),
                        "severity": (loc.get("severity") or dmg.get("severity") or "").replace("SEVERITY_", "").replace("_", " ").lower(),
                        "group": dmg.get("group"),
                    })
                    n_filtered_out["kept"] += 1

                # Nur Cases mit verbleibenden Damages
                if dmgs:
                    cases_meta[cid] = dmgs
        except Exception:
            cases_meta = {}

        # Photos pro Case aus Filesystem — nur für Cases die den Filter überlebt haben
        plate_dir = ROOT / "damage_photos" / plate_safe
        if plate_dir.exists() and plate_dir.is_dir():
            for case_dir in sorted(plate_dir.iterdir()):
                if not case_dir.is_dir(): continue
                cid = case_dir.name
                if cid not in cases_meta:
                    continue  # Case wurde rausgefiltert
                photos = []
                for photo_file in sorted(case_dir.iterdir()):
                    if photo_file.suffix.lower() not in (".jpg", ".jpeg"): continue
                    name = photo_file.stem
                    if "__" in name:
                        ptype, pid = name.split("__", 1)
                    else:
                        ptype, pid = "UNKNOWN", name
                    photos.append({
                        "type": ptype,
                        "photo_id": pid,
                        "url": f"/api/damage_photos/{plate_safe}/{cid}/{photo_file.name}",
                        "case_id": cid,
                    })
                damage_cases_full.append({
                    "case_id": cid,
                    "damages": cases_meta[cid],
                    "photos": photos,
                })

        return {
            "car": dict(car),
            "images": images,
            "damage_cases": damage_cases_full,
            "filter_stats": {
                "enabled": DAMAGE_FILTER_ENABLED,
                "excluded_groups": list(EXCLUDED_DAMAGE_GROUPS),
                "filtered_out_by_group": n_filtered_out["by_group"],
                "filtered_out_by_time": n_filtered_out["by_time"],
                "kept": n_filtered_out["kept"],
                "photo_ts": photo_ts if 'photo_ts' in dir() else None,
            },
        }


@app.get("/api/damage_photos/{plate_safe}/{case_id}/{filename}")
def serve_damage_photo(plate_safe: str, case_id: str, filename: str):
    """Liefert Damage-Foto aus dem Filesystem."""
    path = ROOT / "damage_photos" / plate_safe / case_id / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "damage photo not found")
    return FileResponse(path)


# === Images ===
@app.get("/api/images/{image_id}/file")
def get_image_file(image_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        return FileResponse(row["path"])


@app.get("/api/images/{image_id}")
def get_image_meta(image_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT i.*, c.plate_original FROM images i JOIN cars c ON i.plate_safe = c.plate_safe WHERE i.id = ?",
            (image_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        return dict(row)


# === Annotations ===
@app.get("/api/images/{image_id}/annotations")
def get_annotations(image_id: int):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE image_id = ? ORDER BY created_at",
            (image_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/annotations")
def create_annotation(a: AnnotationIn):
    if a.label not in DAMAGE_CLASSES:
        raise HTTPException(400, f"Label muss eins von {DAMAGE_CLASSES} sein")
    now = time.time()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO annotations (image_id, x, y, w, h, label, severity, paint_damaged, matched_db_damage_id, annotator, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (a.image_id, a.x, a.y, a.w, a.h, a.label, a.severity,
             int(a.paint_damaged) if a.paint_damaged is not None else None,
             a.matched_db_damage_id, a.annotator, a.source, now, now)
        )
        conn.commit()
        return {"id": cur.lastrowid, **a.model_dump()}


@app.patch("/api/annotations/{anno_id}")
def update_annotation(anno_id: int, upd: AnnotationUpdate):
    with get_db() as conn:
        existing = conn.execute("SELECT * FROM annotations WHERE id = ?", (anno_id,)).fetchone()
        if not existing:
            raise HTTPException(404)
        updates = {k: v for k, v in upd.model_dump(exclude_none=True).items()}
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            vals = list(updates.values()) + [time.time(), anno_id]
            conn.execute(f"UPDATE annotations SET {sets}, updated_at = ? WHERE id = ?", vals)
            conn.commit()
        return dict(conn.execute("SELECT * FROM annotations WHERE id = ?", (anno_id,)).fetchone())


@app.delete("/api/annotations/{anno_id}")
def delete_annotation(anno_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM annotations WHERE id = ?", (anno_id,))
        conn.commit()
        return {"deleted": anno_id}


# === Predictions (Multi-Model) ===
VIEW_DESC = {
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


def build_prompt(view):
    desc = VIEW_DESC.get(view, view)
    return f"""You're inspecting a rental car. The photo shows: {desc}.

Find ALL visible damages on the car.
Classes: scratch | stone_chip | dent | crack | missing | major | other.

For each damage output a tight bounding box [ymin, xmin, ymax, xmax] in 0-1000 normalized coords.

Respond ONLY with JSON:
{{
  "damages": [
    {{"bbox_2d": [ymin, xmin, ymax, xmax], "label": "scratch", "confidence": 0.85, "severity": "medium", "reasoning": "Brief description"}}
  ]
}}

If no damages visible, return {{"damages": []}}."""


def build_prompt_cot(view):
    """Chain-of-Thought Prompt mit anti-hallucination rules."""
    desc = VIEW_DESC.get(view, view)
    return f"""You are a CAREFUL Sixt vehicle damage inspector examining a rental car photo.
The photo shows: {desc}.

# Damage Types (study these carefully)
- **scratch**: clear line-like marks on paint with visible PAINT DISRUPTION. Length > 2cm. Must show actual surface damage, not just a reflection line.
- **stone_chip**: small (1-5mm) impact marks. Must have a CLEAR DARK CENTER or starburst pattern showing paint disruption. NOT mere bright spots from light.
- **dent**: clear deformation of metal panel, visible as 3D shape distortion. NOT just shadows.
- **crack**: actual broken glass, plastic, or paint cracks with visible separation.
- **missing**: a part is gone or broken off (cap, badge, trim piece).
- **major**: severe crash damage, deep deformation, hail dents (multiple aligned).
- **other**: graffiti, deep dirt requiring documentation.

# CRITICAL: What is NOT damage (do NOT mark these)
- **Light reflections and highlights**: glossy paint shows bright streaks where light hits — these are NORMAL.
- **Shadow patterns**: dark areas where panels curve are NORMAL, not dents.
- **Dust, water spots, dirt smudges**: these are not paint damage.
- **Color gradients along panel edges**: due to lighting, not damage.
- **Pattern of similar "marks" following a curve or line**: this is almost certainly a REFLECTION on the hood/roof/door, not 20 stone chips.
- **Reflections of the surrounding environment** (other cars, ceiling lights, pillars) in the paint.
- **🟤 SPLASH DIRT / MUD SPATTER / ROAD GRIME**: brown/gray/black spatter patterns — especially on:
  * Wheels, rims, tires (very common — almost every car has this)
  * Rear bumper, rear fenders (kicked up while driving)
  * Lower side skirts and rocker panels
  * Behind the wheels (mud trails)
  These look like clusters of small dark spots/streaks. They are DIRT, NOT stone chips, NOT scratches.
  → If you see a cluster of brown/dark spots on or near a wheel/rim/lower bumper, it's road dirt. SKIP IT.
- **🟫 General road salt residue / dust film**: matte gray-white deposits on lower body. Cosmetic dirt, not damage.

# Sanity Check
If you find yourself marking MORE THAN 5 stone_chips in a tight area, STOP and reconsider — that's almost certainly a single light reflection, not damage. A real car rarely has more than 3-5 visible stone chips in one panel.

# Inspection Procedure (THINK step-by-step)
1. **Identify ALL visible panels/parts**: bumper, hood, fenders, doors, windscreen, mirrors, tires, rims, lights, etc.
2. **For EACH panel**: first look at where LIGHT is coming from. Mentally map highlights and shadows.
3. **For wheels/rims/lower bumpers/rear panels**: ask yourself "is this BROWN/GRAY DIRT or is this real damage?" Dirt is matte, has no sharp edges, often forms spray patterns. Real damage has clear paint disruption.
4. **Then look for ACTUAL damage with paint disruption** — not just brightness variation, not dirt.
5. **Only mark with confidence > 0.7** if you're sure it's not a reflection or dirt.
6. **Cap yourself at ~5-10 damages per panel maximum**. If you see more, it's likely a reflection or dirt pattern.

# Output Format
Output ONLY this JSON (no markdown):
{{
  "panels_scanned": ["front bumper", "hood", ...],
  "damages": [
    {{
      "bbox_2d": [ymin, xmin, ymax, xmax],
      "label": "scratch",
      "confidence": 0.85,
      "severity": "light|medium|severe",
      "panel": "driver_door",
      "reasoning": "8cm diagonal scratch on driver door with visible paint disruption — verified NOT a reflection because the line is darker than surrounding paint"
    }}
  ]
}}

bbox_2d MUST be in 0-1000 normalized [ymin, xmin, ymax, xmax].
If no real damages visible, return {{"panels_scanned": [...], "damages": []}}.
QUALITY > QUANTITY. Fewer, high-confidence detections are better than many false positives."""


def encode_image(path, max_side=1280):
    img = Image.open(path)
    if img.mode != "RGB": img = img.convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


def encode_pil(pil_img, max_side=1280):
    img = pil_img
    if img.mode != "RGB": img = img.convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


def make_tiles(image_path: str, grid: int = 3, overlap: float = 0.10):
    """N×N Tiles mit Overlap. Gibt Liste von (PIL.Image, x_offset, y_offset, tw, th) zurück.
    Default 3×3 mit 10% Overlap = 9 Tiles je ~37% von Original."""
    img = Image.open(image_path)
    if img.mode != "RGB": img = img.convert("RGB")
    W, H = img.size
    step_pct = 1.0 / grid
    tw = int(W * (step_pct + overlap))
    th = int(H * (step_pct + overlap))
    tiles = []
    for row in range(grid):
        for col in range(grid):
            x = int(W * step_pct * col) - (int(W * overlap / 2) if col > 0 else 0)
            y = int(H * step_pct * row) - (int(H * overlap / 2) if row > 0 else 0)
            x = max(0, x)
            y = max(0, y)
            x2 = min(W, x + tw)
            y2 = min(H, y + th)
            tile = img.crop((x, y, x2, y2))
            tiles.append((tile, x, y, x2 - x, y2 - y))
    return tiles, W, H


def call_vlm(model_id: str, data_uri: str, prompt_text: str):
    """Generischer VLM-Call mit Reasoning-Modell-Detection."""
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt_text},
        ]}],
        "max_completion_tokens": 8192,
        "response_format": {"type": "json_object"},
    }
    is_reasoning = (model_id.startswith("gpt-5") or "claude-opus-4" in model_id or "claude-sonnet-4" in model_id)
    if not is_reasoning:
        kwargs["temperature"] = 0.1
        kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")
    resp = llm.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        return json.loads(text), resp.usage
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        return (json.loads(m.group(0)) if m else {"damages": []}), resp.usage


def iou_1000(bbox_a, bbox_b):
    """IoU in 0-1000 koordiniert."""
    ya1, xa1, ya2, xa2 = bbox_a
    yb1, xb1, yb2, xb2 = bbox_b
    inter_x1 = max(xa1, xb1); inter_y1 = max(ya1, yb1)
    inter_x2 = min(xa2, xb2); inter_y2 = min(ya2, yb2)
    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1: return 0.0
    inter = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(0, (xa2-xa1)) * max(0, (ya2-ya1))
    area_b = max(0, (xb2-xb1)) * max(0, (yb2-yb1))
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def merge_damages_nms(all_damages: list, iou_thr: float = 0.4):
    """NMS über alle Tile-Detections (sorted by confidence)."""
    sorted_d = sorted(all_damages, key=lambda d: -(d.get("confidence", 0) or 0))
    kept = []
    for d in sorted_d:
        bbox = d.get("bbox_2d")
        if not bbox or len(bbox) != 4: continue
        is_dup = False
        for k in kept:
            if iou_1000(bbox, k["bbox_2d"]) > iou_thr:
                is_dup = True
                break
        if not is_dup:
            kept.append(d)
    return kept


def collapse_reflection_clusters(damages: list, max_per_cluster: int = 5, cluster_radius: int = 120):
    """Density-Filter: wenn >max_per_cluster gleich-typige BBoxes in <cluster_radius (0-1000)
    konzentriert sind, ist es wahrscheinlich eine Reflexion. Kollabiere zu EINER Cluster-BBox
    mit Warning-Label.

    cluster_radius: BBoxes deren Mittelpunkte näher als diese Distanz beieinander liegen
                    werden zusammengefasst (in 0-1000 normalisiertem Raum).
    """
    if len(damages) <= max_per_cluster:
        return damages

    # Pro Klasse gruppieren
    by_class = {}
    for d in damages:
        by_class.setdefault(d.get("label", "?"), []).append(d)

    result = []
    for label, dets in by_class.items():
        if len(dets) <= max_per_cluster:
            result.extend(dets)
            continue

        # BBox-Centers berechnen
        with_centers = []
        for d in dets:
            ymin, xmin, ymax, xmax = d["bbox_2d"]
            cx = (xmin + xmax) / 2
            cy = (ymin + ymax) / 2
            with_centers.append((d, cx, cy))

        # Simples Greedy-Clustering: nimm das nächste, finde alle in Radius, zusammenfassen
        unassigned = list(range(len(with_centers)))
        clusters = []
        while unassigned:
            seed_idx = unassigned[0]
            seed_d, seed_cx, seed_cy = with_centers[seed_idx]
            cluster = [seed_idx]
            remaining = []
            for idx in unassigned[1:]:
                _, cx, cy = with_centers[idx]
                if ((cx - seed_cx) ** 2 + (cy - seed_cy) ** 2) ** 0.5 < cluster_radius:
                    cluster.append(idx)
                else:
                    remaining.append(idx)
            unassigned = remaining
            clusters.append(cluster)

        for cluster_idxs in clusters:
            if len(cluster_idxs) <= max_per_cluster:
                # Normal: alle behalten
                for idx in cluster_idxs:
                    result.append(with_centers[idx][0])
            else:
                # Cluster zu groß → wahrscheinlich Reflexion → kollabieren
                all_bboxes = [with_centers[i][0]["bbox_2d"] for i in cluster_idxs]
                ymins = [b[0] for b in all_bboxes]
                xmins = [b[1] for b in all_bboxes]
                ymaxs = [b[2] for b in all_bboxes]
                xmaxs = [b[3] for b in all_bboxes]
                mega_bbox = [min(ymins), min(xmins), max(ymaxs), max(xmaxs)]
                avg_conf = sum(with_centers[i][0].get("confidence", 0) for i in cluster_idxs) / len(cluster_idxs)
                result.append({
                    "bbox_2d": mega_bbox,
                    "label": label,
                    "confidence": min(0.4, avg_conf * 0.5),  # Confidence runter — wahrscheinlich FP
                    "severity": "uncertain",
                    "reasoning": f"⚠️ Cluster von {len(cluster_idxs)} {label}-Detections in engem Bereich — wahrscheinlich Reflexion oder Lichtspiel, NICHT zwingend echter Schaden.",
                    "_is_cluster": True,
                    "_cluster_size": len(cluster_idxs),
                })
    return result


def call_model_tiled(model_id: str, image_path: str, view: str, grid: int = 3, include_overview: bool = False):
    """N×N Tile-Calls parallel + NMS. Default 3×3 = 9 Tiles.
    Nutzt Chain-of-Thought-Prompt."""
    from concurrent.futures import ThreadPoolExecutor, as_completed as fut_done

    tiles, W, H = make_tiles(image_path, grid=grid)

    # Overview als zusätzliches "Tile" mit voller Bildgröße (Offset 0,0)
    if include_overview:
        full_img = Image.open(image_path).convert("RGB")
        # (PIL, x_off, y_off, tw, th, is_overview)
        tasks = [(full_img, 0, 0, W, H, True)]
        for t in tiles:
            tasks.append((*t, False))
    else:
        tasks = [(*t, False) for t in tiles]

    view_desc = VIEW_DESC.get(view, view)
    prompt_tile = build_prompt_cot(view) + f"\n\nNOTE: This is a ZOOMED-IN TILE of a larger car photo showing the {view_desc}. The car may only be partially visible in this tile. Detect damages within this tile only."
    prompt_overview = build_prompt_cot(view) + f"\n\nNOTE: This is the FULL overview of the car ({view_desc}). Look at the entire car holistically — pay special attention to damages that span multiple panels, asymmetries, and large-scale issues that need full-car context to identify."

    def call_one(idx, task):
        tile_img, x_off, y_off, tw, th, is_overview = task
        data_uri = encode_pil(tile_img, max_side=1280)
        prompt = prompt_overview if is_overview else prompt_tile
        parsed, usage = call_vlm(model_id, data_uri, prompt)
        damages = parsed.get("damages") or parsed.get("visible_damages") or []
        # Tile-BBoxes (0-1000 within tile/overview) → globale BBoxes (0-1000 within full image)
        for d in damages:
            bbox = d.get("bbox_2d")
            if not bbox or len(bbox) != 4: continue
            ymin, xmin, ymax, xmax = bbox
            px_y1 = ymin/1000 * th + y_off
            px_y2 = ymax/1000 * th + y_off
            px_x1 = xmin/1000 * tw + x_off
            px_x2 = xmax/1000 * tw + x_off
            d["bbox_2d"] = [px_y1/H*1000, px_x1/W*1000, px_y2/H*1000, px_x2/W*1000]
            d["_source"] = "overview" if is_overview else f"tile_{idx-1}"
        return damages, usage

    all_damages = []
    total_tokens_in = total_tokens_out = 0
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futures = [ex.submit(call_one, i, t) for i, t in enumerate(tasks)]
        for f in fut_done(futures):
            damages, usage = f.result()
            all_damages.extend(damages)
            if usage:
                total_tokens_in += usage.prompt_tokens
                total_tokens_out += usage.completion_tokens

    nms_merged = merge_damages_nms(all_damages, iou_thr=0.4)
    # Anti-Halluzination: Cluster gleich-typiger Detections kollabieren
    merged = collapse_reflection_clusters(nms_merged, max_per_cluster=5, cluster_radius=120)

    # Stats: wie viele kamen aus Overview vs. Tiles?
    from_overview = sum(1 for d in merged if d.get("_source") == "overview")
    from_tiles = sum(1 for d in merged if d.get("_source", "").startswith("tile"))
    n_clusters = sum(1 for d in merged if d.get("_is_cluster"))

    class Usage:
        prompt_tokens = total_tokens_in
        completion_tokens = total_tokens_out
    return {
        "damages": merged,
        "n_calls": len(tasks),
        "n_pre_nms": len(all_damages),
        "n_post_nms": len(nms_merged),
        "n_after_cluster_filter": len(merged),
        "from_overview": from_overview,
        "from_tiles": from_tiles,
        "n_reflection_clusters": n_clusters,
    }, Usage()


def call_model(model_id: str, image_path: str, view: str):
    if not llm:
        raise HTTPException(500, "LLM_GW_API_KEY nicht gesetzt")
    data_uri = encode_image(image_path)
    t0 = time.time()

    # GPT-5.x Reasoning-Modelle akzeptieren nur Default-Temperature
    kwargs = {
        "model": model_id,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": build_prompt(view)},
        ]}],
        "max_completion_tokens": 8192,
        "response_format": {"type": "json_object"},
    }
    # Reasoning-Modelle (GPT-5.x, Claude Opus 4.x) akzeptieren nur Default-Temperature
    is_reasoning = (
        model_id.startswith("gpt-5") or
        "claude-opus-4" in model_id or
        "claude-sonnet-4" in model_id
    )
    if not is_reasoning:
        kwargs["temperature"] = 0.1
        kwargs["max_tokens"] = kwargs.pop("max_completion_tokens")

    resp = llm.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text.strip(), flags=re.MULTILINE)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        parsed = json.loads(m.group(0)) if m else {"damages": []}
    return parsed, time.time() - t0, resp.usage


@app.get("/api/images/{image_id}/predictions_cached")
def get_predictions_cached(image_id: int):
    """Gibt nur gecachte Predictions zurück, ohne Modell-Call zu triggern.
    Für Auto-Load beim Öffnen eines Bildes."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT model, raw_json, n_damages, latency_s, created_at FROM predictions WHERE image_id = ?",
            (image_id,)
        ).fetchall()
        out = {}
        for r in rows:
            out[r["model"]] = {
                "parsed": json.loads(r["raw_json"]),
                "n_damages": r["n_damages"],
                "latency_s": r["latency_s"],
                "created_at": r["created_at"],
            }
        return out


@app.get("/api/images/{image_id}/predictions")
def get_predictions(image_id: int, model: Optional[str] = None, force: bool = False, tiled: bool = False):
    with get_db() as conn:
        img = conn.execute("SELECT path, view FROM images WHERE id = ?", (image_id,)).fetchone()
        if not img:
            raise HTTPException(404)

        results = {}
        models_to_run = [MODELS[model]] if model and model in MODELS else list(MODELS.values())

        for model_id in models_to_run:
            # Cache-Key: tiled bekommt eigenen Suffix
            cache_model = f"{model_id}#tiled" if tiled else model_id
            cached = conn.execute(
                "SELECT * FROM predictions WHERE image_id = ? AND model = ?",
                (image_id, cache_model)
            ).fetchone()
            if cached and not force:
                results[model_id] = json.loads(cached["raw_json"])
                continue

            try:
                t0 = time.time()
                if tiled:
                    parsed, usage = call_model_tiled(model_id, img["path"], img["view"])
                    latency = time.time() - t0
                else:
                    parsed, latency, usage = call_model(model_id, img["path"], img["view"])
                damages = parsed.get("damages", []) or parsed.get("visible_damages", [])
                raw = json.dumps(parsed)
                if cached:
                    conn.execute(
                        "UPDATE predictions SET raw_json = ?, n_damages = ?, tokens_in = ?, tokens_out = ?, latency_s = ?, created_at = ? WHERE id = ?",
                        (raw, len(damages), usage.prompt_tokens, usage.completion_tokens,
                         latency, time.time(), cached["id"])
                    )
                else:
                    conn.execute(
                        "INSERT INTO predictions (image_id, model, raw_json, n_damages, tokens_in, tokens_out, latency_s, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (image_id, cache_model, raw, len(damages), usage.prompt_tokens, usage.completion_tokens, latency, time.time())
                    )
                conn.commit()
                results[model_id] = parsed
            except Exception as e:
                results[model_id] = {"error": str(e)[:200]}

        return results


# === Export ===
@app.get("/api/export/coco")
def export_coco():
    """COCO-Format Annotation-Export."""
    with get_db() as conn:
        annos = conn.execute(
            "SELECT a.*, i.width, i.height, i.path FROM annotations a JOIN images i ON a.image_id = i.id WHERE a.source = 'human'"
        ).fetchall()

        images_seen = {}
        coco_images = []
        coco_annos = []
        categories = [{"id": i+1, "name": c} for i, c in enumerate(DAMAGE_CLASSES)]
        cat_id = {c: i+1 for i, c in enumerate(DAMAGE_CLASSES)}

        for a in annos:
            img_id = a["image_id"]
            if img_id not in images_seen:
                images_seen[img_id] = len(coco_images) + 1
                coco_images.append({
                    "id": images_seen[img_id],
                    "file_name": Path(a["path"]).name,
                    "width": a["width"],
                    "height": a["height"],
                })
            coco_annos.append({
                "id": a["id"],
                "image_id": images_seen[img_id],
                "category_id": cat_id.get(a["label"], 7),
                "bbox": [a["x"]*a["width"], a["y"]*a["height"],
                         a["w"]*a["width"], a["h"]*a["height"]],
                "area": a["w"]*a["width"] * a["h"]*a["height"],
                "iscrowd": 0,
                "attributes": {"severity": a["severity"], "paint_damaged": bool(a["paint_damaged"])},
            })

        return JSONResponse({
            "info": {"description": "Sixt Damage Detection Annotations"},
            "categories": categories,
            "images": coco_images,
            "annotations": coco_annos,
        })


@app.get("/api/stats")
def stats():
    with get_db() as conn:
        return {
            "cars": conn.execute("SELECT COUNT(*) FROM cars").fetchone()[0],
            "images": conn.execute("SELECT COUNT(*) FROM images").fetchone()[0],
            "human_annotations": conn.execute("SELECT COUNT(*) FROM annotations WHERE source = 'human'").fetchone()[0],
            "predictions_cached": conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0],
            "cars_with_annotations": conn.execute("SELECT COUNT(DISTINCT i.plate_safe) FROM annotations a JOIN images i ON a.image_id = i.id WHERE a.source = 'human'").fetchone()[0],
        }


# === Desktop Frontend ===
@app.get("/")
def root():
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text())


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
