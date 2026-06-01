# Label Studio Multi-Backend Setup

Vergleich von **gemini-3.1-pro** vs **gpt-5.5** auf den Smoke-Test-Bildern.

## Start

```bash
# 1. Env-Var setzen
export LLM_GW_API_KEY=<dein-sixt-llm-gateway-key>

# 2. Docker-Compose hochfahren
cd pipeline/labelstudio
docker-compose up -d --build

# 3. Auf http://localhost:8080 einloggen
#    User:     admin@sixt.com   (siehe docker-compose.yml)
#    Password: aus LABEL_STUDIO_PASSWORD env-var

# 4. Account → Access Token kopieren
export LS_API_TOKEN=<token>

# 5. Projekt einrichten + Bilder hochladen
../../.venv/bin/python setup_project.py
```

## Workflow

1. Öffne Projekt in Label Studio
2. Klicke auf ein Bild
3. Sidebar "Predictions" zeigt beide Modelle separat
4. Klick "Retrieve" für Live-Vorhersage von einem Modell
5. Bulk: Project Settings → Model → "Get predictions for all tasks"

## Auswertung (nach manueller Annotation)

```python
# Per-Model Scoring vs. Ground-Truth-Annotations
from label_studio_sdk import Client
ls = Client(url="http://localhost:8080", api_key="...")
project = ls.get_project(PROJ_ID)

for task in project.get_tasks():
    gt = task["annotations"][0]["result"]      # Mensch-Label
    pred_gemini = [p for p in task["predictions"] if p["model_version"] == "gemini-3.1-pro"]
    pred_openai = [p for p in task["predictions"] if p["model_version"] == "gpt-5.5"]
    # IoU, Class-Acc, etc.
```

## Stoppen

```bash
docker-compose down
```

## Bilder neu laden

```bash
docker-compose down -v   # ⚠️ löscht Annotations!
docker-compose up -d
```
