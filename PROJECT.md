# Sixt Live Damage Detection — Projekt-Übersicht

Ein Zwei-Komponenten-System für die Erkennung von Schäden an Sixt-Mietwagen
mit Multimodal-LLMs (Gemini 3.1 Pro / 3.5 Flash) über das interne Sixt
LLM-Gateway.

```
┌──────────────────────┐         ┌──────────────────────┐
│  Android-App         │         │  Web-Annotation-Tool │
│  (Samsung A33 Live)  │         │  (Mac, Eval/Compare) │
│                      │         │                      │
│  Kamera → Gemini     │         │  Datasets → 4 VLMs   │
│  → BBox-Overlay      │         │  → BBox-Vergleich    │
└──────────┬───────────┘         └──────────┬───────────┘
           │                                │
           │  HTTPS (Bearer-Key)            │  Python-OpenAI-Client
           │                                │
           ▼                                ▼
      ┌──────────────────────────────────────────┐
      │  Sixt LLM-Gateway                        │
      │  https://llm.orange.sixt.com/v1          │
      │  → Vertex AI Gemini / Anthropic / OpenAI │
      └──────────────────────────────────────────┘
```

Beide Tools teilen sich denselben CoT-Prompt mit Anti-Halluzinations-Regeln,
denselben Damage-Filter (Scheiben/Räder raus, Zeitfilter), und dieselbe
Master-Taxonomie (`scratch | stone_chip | dent | crack | missing | major | other`).

---

# Teil 1: Android-App (Sixt Damage Scanner)

**Ort:** [SixtDamageScanner/](SixtDamageScanner/)
**Stack:** Kotlin · Jetpack Compose · CameraX · OkHttp · Coil
**Backend:** keiner — die App spricht direkt mit dem Sixt LLM-Gateway

## Architektur

```
┌─────────────────────────────────────────────────────────┐
│  MainActivity.kt                                        │
│  └── AppRoot (NavHost)                                  │
│      ├── CaptureScreen      ← live camera + review      │
│      ├── ResultsScreen      ← Liste + BBox-Overlays     │
│      └── SettingsScreen     ← API-Key + Endpoint        │
├─────────────────────────────────────────────────────────┤
│  ScanViewModel.kt           UI-State + Coroutines       │
│  ├── jobs: Map<view, Job>   Pro Foto eine Coroutine     │
│  └── logger: SessionLogger  Schreibt Sessions raus      │
├─────────────────────────────────────────────────────────┤
│  llm/DamageAnalyzer.kt      Tile-Splitter + NMS + Filter│
│  llm/LlmGatewayClient.kt    OkHttp + JSON-Parsing       │
│  llm/DamagePrompt.kt        CoT-Prompt mit View-Desc    │
├─────────────────────────────────────────────────────────┤
│  logging/SessionLogger.kt   Datei-basierte Persistenz   │
│  logging/Telemetry.kt       Data classes + JSON-Serial. │
│  logging/Pricing.kt         Token → USD                 │
└─────────────────────────────────────────────────────────┘
```

## User-Flow

```
App-Start
  │
  ▼
Capture-Screen (Live-Kamera direkt an)
  │
  │  ⚙️ ───► Settings (API-Key + Endpoint)
  │
  │  [Pro] [Flash]   [📐 1280] [Tile 3×3] [↻ Reset]   ← Top-Pills (live editable)
  │  ●●●○○○○○○○○○                                       ← Progress-Dots (12 views)
  │
  ▼
  📷 Auslöser drücken
  │   ├─► CameraX speichert JPEG in cacheDir/captures/
  │   └─► vm.submitPhoto() startet Background-Coroutine
  ▼
Review-Mode (gleicher Screen, Foto over-layed über Live-Preview)
  │
  │  [↻ Wiederholen]  [Weiter →]
  │
  │  Im Hintergrund läuft die Analyse → Resultat landet im StateFlow,
  │  Sessions-Log wird inkrementell aktualisiert
  │
  ▼
12× wiederholt
  │
  ▼
"Auswertung →"
  │   └─► vm.finalizeSession() schreibt session.json
  ▼
Results-Screen
  ├── Stats-Card (#Bilder, #Schäden, #Pending)
  ├── PhotoResultCard pro Foto mit Canvas-BBoxes
  └── Logs-Pfad-Hinweis ("adb pull …")
```

## Die 12 Views

```
FRONT_STRAIGHT     · DIAGONAL_FRONT_LEFT  · DIAGONAL_FRONT_RIGHT
SIDE_LEFT          · SIDE_RIGHT
DIAGONAL_REAR_LEFT · DIAGONAL_REAR_RIGHT  · REAR_STRAIGHT
TYRE_FRONT_LEFT    · TYRE_FRONT_RIGHT
TYRE_REAR_LEFT     · TYRE_REAR_RIGHT
```

## Settings im Detail

| Pill | Was | Werte |
|---|---|---|
| `[Pro] / [Flash]` | Modell | `vertex_ai/gemini-3.1-pro` vs `vertex_ai/gemini-3.5-flash` |
| `[📐 1280 / 2048 / Max]` | Resize-Cap vor Send | 1280px · 2048px · 4000px (≈ Original) |
| `[Tile 3×3]` | Multi-Call-Mode | aus = 1 Call · ein = 9 parallele Calls + NMS + Cluster |
| `[↻]` | Reset | Finalisiert laufende Session, leert UI |

Alle 4 Pills **live während des Scans** umschaltbar — der nächste Auslöser
verwendet die aktuellen Werte. Jedes Foto loggt seine eigene Konfiguration.

## Anti-Halluzinations-Architektur (3 Layer Defense)

Genau dieselbe wie im Web-Tool:

1. **Smart CoT-Prompt** ([DamagePrompt.kt:21](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/DamagePrompt.kt))
   - "Klassen-Definition: was IST scratch/stone_chip/etc."
   - "Was ist NICHT damage: Reflexionen, Schmutz-Spatter, Schatten…"
   - Sanity-Check: "Mehr als 5 stone_chips in einem Bereich? STOP — das ist Reflexion."
   - Inspection-Procedure: Schritt-für-Schritt-Anleitung

2. **NMS (IoU > 0.4)** ([DamageAnalyzer.kt:144](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/DamageAnalyzer.kt))
   - Im Tile-Mode finden mehrere Tiles dieselben Schäden in Überlappungs-Zonen
   - Höchste Confidence gewinnt, Duplikate werden verworfen

3. **Reflection-Cluster-Collapse** ([DamageAnalyzer.kt:155](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/DamageAnalyzer.kt))
   - Wenn >5 gleich-typige BBoxes in <120 Px Cluster-Radius
   - → kollabiert zu **EINER** Cluster-BBox mit Warnung "wahrscheinlich Reflexion"
   - Confidence auf 0.4 gecapt, Cluster-Größe wird gemerkt

## Live-Konfigurations-Vergleich (Cost / Latenz / Qualität)

Pro 12-Foto-Session am Sixt-A33:

| Kombi | Prompt-Tokens | Flash | Pro | Latenz | Anwendung |
|---|---|---|---|---|---|
| Single 1280 | ~37k | $0.003 | $0.05 | ~5 s | **Standard** — schnell, günstig |
| Single 2048 | ~43k | $0.003 | $0.05 | ~6 s | Bessere Stone-Chip-Detection |
| Single Max | ~99k | $0.007 | $0.12 | ~10 s | Premium-Single |
| Tile 1280 | ~244k | $0.018 | $0.30 | ~6 s | (selten sinnvoll, zu klein) |
| Tile 2048 | ~272k | $0.020 | $0.34 | ~8 s | **Premium-Scan** — Sweet Spot |
| Tile Max | ~383k | $0.029 | $0.48 | ~14 s | Forensik / Streit-Fälle |

**Sweet-Spot-Empfehlung:** `Flash · Single · 2048` für Volumen,
`Pro · Tile · 2048` für hochpreisige Fahrzeuge bei Rückgabe.

## Session-Logging — wie + wo

**Ort auf Handy:**
```
/sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/
└── 2026-06-01_18-04-22_M-AB1234_a3f8c1/
    ├── session.json          ← Aggregate (am Ende)
    ├── photos.json           ← Liste (nach jedem Foto fortgeschrieben)
    ├── 01_FRONT_STRAIGHT.jpg ← Original-JPEG
    └── ...
```

**Auf den Mac holen:**
```bash
adb=/Users/g227939/Library/Android/sdk/platform-tools/adb
$adb pull /sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/ ./scanner-export/
```

**`session.json` enthält:**
- Session-ID, Plate, Modell, Tile-Mode
- Start/End-Timestamp, Device-Info
- Aggregate: `n_photos`, `n_damages_total`, `total_bytes_sent`, `total_prompt_tokens`,
  `total_completion_tokens`, `total_usd_estimated`, `avg_latency_s`, `p95_latency_s`

**`photos.json[N]` pro Foto enthält:**
- `original_resolution` (z.B. `[4080, 3060]`) und `sent_resolution` (z.B. `[1280, 960]`)
- `model`, `model_id`, `tile_mode`, `max_side_setting` ← pro Foto separat!
- `calls[]` — pro HTTP-Call ein Eintrag mit:
  - `tile_idx` (`null` = single, `0..8` = tile)
  - `http_status`, `bytes_sent`, `bytes_received`
  - `latency_ms` (pro Call gemessen, nicht aggregiert)
  - `prompt_tokens`, `completion_tokens` (aus Gemini-`usage`-Feld)
  - `error` (z.B. `"HTTP 401: …"`)
- `totals` — Summe + USD-Schätzung
- `analysis` — `n_pre_nms`, `n_post_nms`, `n_after_cluster_filter`, `damages[]`
  - jeder Damage mit `bbox_2d`, `label`, `confidence`, `severity`, `panel`, `reasoning`, `source` (Tile-Index)

**Was NICHT gespeichert wird (bewusst):**
- Resized-JPEG (was Gemini sah) — fliegt nach base64-Encode weg
- Annotated-PNG mit eingebrannten BBoxes — offline rekonstruierbar aus Original + JSON
- Roh-Gemini-JSON-Response — nur geparste Damages
- API-Key (logisch)

## Build & Install

```bash
cd /Users/g227939/Documents/Code/get_anglepicture_aftercleaning/SixtDamageScanner

# Build (~20 s wenn Caches warm)
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug

# Install
adb=/Users/g227939/Library/Android/sdk/platform-tools/adb
$adb install -r app/build/outputs/apk/debug/app-debug.apk

# Launch
$adb shell am start -n com.sixt.damagescanner/.MainActivity
```

API-Key ist als Default im Code hardcoded (`ScanViewModel.kt:50`) — kann
in Settings überschrieben werden. Für externe Verteilung muss der Default raus.

## Code-Hotspots

| Datei | Was darin steckt |
|---|---|
| [MainActivity.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/MainActivity.kt) | 4 Screens, Pill-Komponenten, BBox-Rendering via Canvas |
| [ScanViewModel.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/ScanViewModel.kt) | UiState, Submit-Logik mit Stale-Guard, Logger-Lifecycle |
| [llm/DamageAnalyzer.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/DamageAnalyzer.kt) | Tile-Splitter, parallele Coroutines, NMS, Cluster-Filter |
| [llm/LlmGatewayClient.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/LlmGatewayClient.kt) | OkHttp-Call, Telemetry-Capture, JSON-Parsing |
| [llm/DamagePrompt.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/llm/DamagePrompt.kt) | CoT-Prompt + 12 View-Descriptions |
| [logging/SessionLogger.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/logging/SessionLogger.kt) | File-IO, Ordner-Anlage, inkrementelles photos.json |
| [logging/Telemetry.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/logging/Telemetry.kt) | Data classes für Call/Photo/Session, org.json-Serialisierung |
| [logging/Pricing.kt](SixtDamageScanner/app/src/main/java/com/sixt/damagescanner/logging/Pricing.kt) | Token-zu-USD-Rechnung |

---

# Teil 2: Web-Annotation-Tool

**Ort:** [pipeline/annotation_tool/](pipeline/annotation_tool/)
**Stack:** FastAPI · SQLite · Konva.js · Pillow · OpenAI-SDK
**Zweck:** VLM-Vergleich an einem festen Datensatz für Pre/Post-Eval

## Was es macht

Eine lokale Web-App auf dem Mac, mit der du:

1. Aus dem Sixt-Datensatz (16k exterior_photos + 41k damage_photos) ein **Auto +
   Foto auswählst** ([frontend/index.html](pipeline/annotation_tool/frontend/index.html))
2. Die Datenbank-Schäden ("Ground-Truth-Hint", aus `damage_cases.json`) drüberlegen kannst
3. **4 VLMs gleichzeitig** anfeuerst und ihre BBoxes vergleichst:
   - `gemini` → `vertex_ai/gemini-3.1-pro`
   - `flash` → `vertex_ai/gemini-3.5-flash`
   - `gpt5` → `openai/gpt-5.5`
   - `claude` → `anthropic/claude-opus-4`
4. Single-Mode vs **Tile-Mode 3×3** vergleichst
5. Eigene manuelle BBoxes annotierst (für späteres Fine-Tuning-Dataset)
6. Predictions im SQLite-Cache hältst — wiederholte Auswertung kostet nichts

## Architektur

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (localhost:8000)                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Konva.js Canvas — Zoom + Pan, BBox-Drawing            │  │
│  │  Multi-Model-Toggles: Pro · Flash · GPT · Claude       │  │
│  │  DB-Damages-Layer (gefiltert)                          │  │
│  │  Manuelle Annotation-Layer                             │  │
│  └────────────────────────────────────────────────────────┘  │
└────────────────────────┬─────────────────────────────────────┘
                         │ REST / JSON
                         ▼
┌──────────────────────────────────────────────────────────────┐
│  FastAPI (backend/main.py, 1321 Zeilen)                      │
│  ├── /api/cars                  Liste der 500 Plates         │
│  ├── /api/cars/{plate}          Fotos + Damages + Cases      │
│  ├── /api/images/{id}           Single-Image-Metadata        │
│  ├── /api/images/{id}/file      JPEG-Stream                  │
│  ├── /api/annotations           CRUD für manuelle BBoxes     │
│  ├── /api/images/{id}/predictions          on-demand VLM     │
│  ├── /api/images/{id}/predictions_cached   SQLite-Cache      │
│  ├── /api/export/coco           COCO-JSON-Export             │
│  └── /api/stats                 Annotation-Statistiken       │
├──────────────────────────────────────────────────────────────┤
│  SQLite (annotations.db)                                     │
│  ├── annotations   manuelle BBoxes                           │
│  └── predictions   Cached VLM-Outputs (4 Modelle × 2 Modes)  │
├──────────────────────────────────────────────────────────────┤
│  Daten on-disk                                               │
│  ├── exterior_photos/  16.906 Bilder (10 Winkel)             │
│  ├── damage_photos/    41.963 Bilder (3 Typen)               │
│  ├── damage_cases.json   1.637 Plates, 9.780 Damages         │
│  └── photos_export.json  1.094 Cleaning-Tasks                │
└──────────────────────────────────────────────────────────────┘
```

## VLM-Prompt + Filter (gleiche Logik wie App)

Im Backend in [backend/main.py](pipeline/annotation_tool/backend/main.py):

- `build_prompt_cot(view)` ([main.py:629](pipeline/annotation_tool/backend/main.py)) — derselbe CoT-Prompt wie in der App
- `call_vlm(model_id, data_uri, prompt_text)` ([main.py:740](pipeline/annotation_tool/backend/main.py)) — Reasoning-Modell-Detection (GPT-5/Claude-4 brauchen `max_completion_tokens`, kein `temperature`)
- `call_model_tiled(...)` ([main.py:867](pipeline/annotation_tool/backend/main.py)) — Tile-Mode 3×3 mit `ThreadPoolExecutor`
- `merge_damages_nms()` ([main.py:768](pipeline/annotation_tool/backend/main.py)) — NMS IoU > 0.4
- `collapse_reflection_clusters()` ([main.py:794](pipeline/annotation_tool/backend/main.py)) — Cluster-Filter

## Damage-Filter ("welche DB-Schäden sind hier sichtbar?")

In der Web-App siehst du nur die Schäden, die **zum Foto passen**:

1. **Räder + Scheiben raus** — Master-Taxonomie deckt nur Karosserie ab
2. **Zeit-Filter** — Schaden muss **vor** Foto-Zeitstempel angelegt worden sein
   (sonst sind im Foto Schäden zu sehen, die erst später passiert sind)
3. **View-Filter** — `coordinates[].projection` muss zum Winkel passen

Aus den ursprünglich 9.780 Damages werden so im Schnitt nur 2-4 pro Foto
angezeigt — die wirklich relevanten.

## Setup

```bash
cd /Users/g227939/Documents/Code/get_anglepicture_aftercleaning/pipeline/annotation_tool

# Python-Venv + Dependencies
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn pillow python-multipart openai

# .env mit API-Key
echo "LLM_GW_API_KEY=<dein-sixt-llm-gateway-key>" > .env

# Backend starten
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Browser öffnen
open http://localhost:8000/
```

## UI im Browser

```
┌──────────────────────────────────────────────────────────┐
│  Plate-Selector ▼                  Foto-Thumbnails-Bar   │
│  M-AB 1234 (BMW 3er, Ext)          [thumb1][thumb2]...   │
├──────────────────────────────────────────────────────────┤
│                                          ┌─────────────┐ │
│                                          │ DB-Damages  │ │
│                                          │ ▢ scratch   │ │
│                                          │ ▢ dent      │ │
│        Konva.js Canvas                   ├─────────────┤ │
│        (zoom + pan + draw)               │ Predictions │ │
│                                          │ ☑ Pro       │ │
│        Bild mit BBoxes-Layer             │ ☑ Flash     │ │
│                                          │ ☐ GPT-5.5   │ │
│                                          │ ☐ Claude    │ │
│                                          │ Tile-Mode ☑ │ │
│                                          └─────────────┘ │
│                                          [▶ Analyze]    │
│                                          [💾 Save Anno] │
└──────────────────────────────────────────────────────────┘
```

## Cache-Strategie

Predictions sind teuer (jeder Klick = ~5-15 s Gemini-Call, 9× bei Tile).
Deswegen wird jedes Resultat in SQLite gecacht und mit
`(image_id, model, tile_mode)` als Key wieder ausgespielt.

Endpoint `predictions_cached` ([main.py:980](pipeline/annotation_tool/backend/main.py))
liefert sofort — `predictions` ([main.py:1000](pipeline/annotation_tool/backend/main.py))
schießt frisch ab.

## Export für ML-Training

Wenn du genug manuelle Annotationen angesammelt hast:

```bash
curl http://localhost:8000/api/export/coco > my_dataset.json
```

Liefert ein COCO-Format-JSON mit Bildern + Bounding-Boxes + Kategorien —
direkt verwendbar für YOLOv11-Training.

## Zentrale Datei-Liste

| Datei | Funktion |
|---|---|
| [backend/main.py](pipeline/annotation_tool/backend/main.py) | FastAPI-App, ~20 Endpoints, VLM-Calls, SQLite-Schema |
| [frontend/index.html](pipeline/annotation_tool/frontend/index.html) | Single-Page-UI |
| [frontend/app.js](pipeline/annotation_tool/frontend/app.js) | Konva.js-Wiring, Multi-Layer-Rendering, Toggle-Logik |
| [backend/run_test_predictions.py](pipeline/annotation_tool/backend/run_test_predictions.py) | Batch-VLM-Eval auf vorausgewähltem Test-Set |
| `backend/data/annotations.db` | SQLite-DB (manuelle Annos + Prediction-Cache) |

---

# Teil 3: Daten-Pipeline (Vorab)

Die Bilder + Damage-Metadaten kamen ursprünglich aus Sixt-Lynx via gRPC:

| Script | Was es macht |
|---|---|
| [getPictures.py](getPictures.py) | Lädt Cleaning-Task-Fotos via `vehicle_tasks_management.api` |
| [getDamageCases.py](getDamageCases.py) | Lädt Damage-Cases + Coordinates via Damage-API |
| [download_exterior.py](download_exterior.py) | Bulk-Download exterior_photos für branch_id=11 München |
| [download_damages.py](download_damages.py) | Bulk-Download damage_photos (Achtung: WebP-als-`.jpeg`-Bug, ~54%) |
| [DATASET_STATUS.md](DATASET_STATUS.md) | Aktueller Stand des Datenbestands |

**Aktueller Datenbestand (München, branch_id=11):**
- 500 stratifizierte Autos, 6.202 exterior_photos
- 41.963 damage_photos
- 9.780 Damages → **ohne Scheiben/Räder ohne nach-Foto-Schäden: ~3.000 sichtbare GT-Schäden**

---

# Teil 4: Wie spielen App und Web-Tool zusammen?

```
┌────────────────────────┐                    ┌────────────────────────┐
│  Web-Annotation-Tool   │                    │  Android-App           │
│  (offline Eval)        │                    │  (live in der Hand)    │
└──────────┬─────────────┘                    └──────────┬─────────────┘
           │                                              │
           │  Großer Datensatz (16k Bilder)               │  Live-Kamera (12 Fotos)
           │  Manuelle Annotation möglich                 │  Keine GT, keine Annotation
           │  Cache + Vergleich von 4 Modellen            │  1 Modell live wählbar
           │  Tile vs Single, Pro vs Flash                │  Tile vs Single + MaxSide
           │  SQLite + Konva.js                           │  JSON + Files
           │                                              │
           │  → ENTSCHEIDET: welches Modell + Mode        │  → AUSGEFÜHRT: die Wahl, im Feld
           └──────────────────────────────────────────────┘
```

**Workflow:**

1. **Im Web-Tool** an ~50-100 Auto-Sessions mit GT bekannten Schäden testen:
   "Erkennt das Modell die richtigen Schäden? Welche Mode-Kombi ist Sweet-Spot?"
2. **Empfehlung ableiten:** z.B. "Flash · Single · 2048 für Volumen-Scans"
3. **In der App** als Standard-Setting setzen (`UiState.maxSide = 2048` Default ändern)
4. **Live im Feld** scannen mit dieser Setting
5. **Session-Logs zurück** auf den Mac per `adb pull`
6. **Mit Python/jq** auswerten:
   - Wie oft kommt Tile-Mode zum Einsatz?
   - Welche Views haben höchste FP-Rate?
   - Wie gut ist die Latenz im realen Mobilfunk?
7. **Iteration** — Prompt anpassen, Pricing-Rates aktualisieren, etc.

---

# Teil 5: Bekannte offene Punkte

| Thema | Status |
|---|---|
| **Sixt-internes Gateway-Pricing** | Hardcoded mit Public-Google-Rate, könnte abweichen |
| **License-Plate-Blurring** | Nicht implementiert, relevant bei externem Roll-Out |
| **Annotated-PNG mit BBox** | Nicht gespeichert, offline aus Original + JSON rekonstruierbar |
| **In-App History-UI** | Bewusst nicht — reine File-basierte Analyse |
| **Backend für App** | Bewusst entfernt — App spricht direkt mit Gateway |
| **YOLO-Training-Pipeline** | Plan steht ([damage_detection_plan.md](.claude/plans/der-grund-warum-ich-imperative-sparkle.md)), nicht implementiert |
| **Pre/Post-Rental-Vergleich** | Nicht implementiert — App detectet alle sichtbaren Schäden |
| **Hardcoded API-Key in APK** | Nur OK für interne Demo; vor Verteilung raus |

---

# Teil 6: Schnellreferenz Commands

```bash
# Web-Annotation-Tool starten
cd pipeline/annotation_tool && source venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
open http://localhost:8000/

# App bauen + installieren
cd SixtDamageScanner
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug
adb=/Users/g227939/Library/Android/sdk/platform-tools/adb
$adb install -r app/build/outputs/apk/debug/app-debug.apk

# Sessions vom Handy holen
$adb pull /sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/ ./scanner-export/

# Sessions analysieren (jq)
for d in scanner-export/*/; do
  jq -r '.aggregates | "\(.n_photos)f \(.n_damages_total)d $\(.total_usd_estimated) \(.avg_latency_s)s"' $d/session.json
done

# Tile-Mode-Effektivität pro Tile
for d in scanner-export/*/; do
  jq -r '.[] | .calls[] | "\(.tile_idx)\t\(.latency_ms)\t\(.completion_tokens)"' $d/photos.json
done

# Export aus Web-Tool als COCO-Dataset
curl http://localhost:8000/api/export/coco > my_dataset.json
```
