"""Setup-Skript: Erstellt Label-Studio-Projekt, registriert beide ML-Backends, lädt 50 Bilder.

Voraussetzung: docker-compose up gestartet, Label Studio läuft auf localhost:8080.
Lege erst einen User an (admin@sixt.com / $LABEL_STUDIO_PASSWORD) — der wird beim ersten Start automatisch erstellt via Env vars.
Hole danach das API-Token aus dem UI (User → Account → Access Token) und setze es:
  export LS_API_TOKEN=<your-token>
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

LS_URL = os.environ.get("LS_URL", "http://localhost:8080")
LS_TOKEN = os.environ.get("LS_API_TOKEN")
if not LS_TOKEN:
    print("❌ LS_API_TOKEN nicht gesetzt.\n"
          "   1) Öffne http://localhost:8080 und logge ein (admin@sixt.com / $LABEL_STUDIO_PASSWORD)\n"
          "   2) Account → Access Token → kopiere\n"
          "   3) export LS_API_TOKEN=<token>\n")
    sys.exit(1)

HEADERS = {"Authorization": f"Token {LS_TOKEN}"}

# Project-Config: 7 Damage-Klassen + Severity-Attribut
LABEL_CONFIG = """<View>
  <Image name="image" value="$image" zoom="true" zoomControl="true"/>
  <RectangleLabels name="label" toName="image" canRotate="false">
    <Label value="scratch" background="#ff5050"/>
    <Label value="stone_chip" background="#ffa000"/>
    <Label value="dent" background="#3cb4ff"/>
    <Label value="crack" background="#c83cff"/>
    <Label value="missing" background="#ff3cc8"/>
    <Label value="major" background="#ff143c"/>
    <Label value="other" background="#787878"/>
  </RectangleLabels>
  <Choices name="severity" toName="image" perRegion="true" required="false">
    <Choice value="light"/>
    <Choice value="medium"/>
    <Choice value="severe"/>
  </Choices>
  <Choices name="paint_damaged" toName="image" perRegion="true" required="false">
    <Choice value="yes"/>
    <Choice value="no"/>
  </Choices>
</View>"""


def safe(name): return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def create_project():
    r = requests.post(
        f"{LS_URL}/api/projects",
        headers=HEADERS,
        json={
            "title": "Damage Detection — Multi-Model Smoke Test",
            "label_config": LABEL_CONFIG,
            "description": "Vergleich gemini-3.1-pro vs gpt-5.5 auf exterior_photos",
        },
    )
    r.raise_for_status()
    proj = r.json()
    print(f"✅ Projekt erstellt: ID {proj['id']}")
    return proj


def register_ml_backend(project_id, url, title):
    r = requests.post(
        f"{LS_URL}/api/ml/",
        headers=HEADERS,
        json={
            "project": project_id,
            "url": url,
            "title": title,
            "description": f"VLM-Backend: {title}",
            "auto_update": True,
        },
    )
    if r.status_code >= 300:
        print(f"  ✗ ML-Backend {title}: {r.status_code} {r.text[:200]}")
        return None
    print(f"  ✓ ML-Backend registriert: {title}")
    return r.json()


def upload_images(project_id, image_paths):
    """Lädt Bilder als Tasks mit local-files URL hoch."""
    tasks = []
    for p in image_paths:
        rel = str(p).split("exterior_photos/", 1)[-1]
        tasks.append({
            "data": {"image": f"/data/local-files/?d=exterior_photos/{rel}"}
        })
    # Bulk-Import
    r = requests.post(
        f"{LS_URL}/api/projects/{project_id}/import",
        headers=HEADERS,
        json=tasks,
    )
    r.raise_for_status()
    print(f"✅ {len(tasks)} Tasks importiert")


def load_smoke_50_paths():
    """Holt die 50 Bilder die im Smoke Test verwendet wurden (alle 10 Autos)."""
    output_file = Path(__file__).parent.parent / "quality" / "smoke10_outputs.json"
    with open(output_file) as f:
        data = json.load(f)
    # Alle 100 Bilder (10 Autos × 10 Views)
    return list(data.keys())


if __name__ == "__main__":
    # Check Label Studio erreichbar
    try:
        r = requests.get(f"{LS_URL}/api/projects", headers=HEADERS, timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"❌ Label Studio nicht erreichbar auf {LS_URL}: {e}")
        sys.exit(1)

    proj = create_project()
    proj_id = proj["id"]

    print("\nRegistriere ML-Backends ...")
    # Vom Host aus: localhost:9090/9091
    # Aus dem LS-Container: http://ml_gemini:9090 / http://ml_openai:9091
    register_ml_backend(proj_id, "http://ml_gemini:9090", "gemini-3.1-pro")
    register_ml_backend(proj_id, "http://ml_openai:9091", "gpt-5.5")

    print("\nLade Smoke-Test-Bilder hoch ...")
    paths = load_smoke_50_paths()
    print(f"  {len(paths)} Bilder gefunden")
    upload_images(proj_id, paths)

    print(f"\n✅ Setup fertig!")
    print(f"   Öffne: {LS_URL}/projects/{proj_id}/data")
    print(f"   Klicke ein Task → 'Retrieve predictions' → beide Backends laufen")
    print(f"   Oder: Bulk-Predict via 'Get predictions' in Project Settings")
