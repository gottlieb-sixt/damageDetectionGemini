# Dataset Status — Situation · Complication · Solution

## SITUATION — Was wir haben

### Daten-Asset (Stand 2026-06-01)

| Quelle | Volumen | Inhalt |
|---|---|---|
| **photos_export.json** | 1.845 Plates · 2.100 Cleaning-Tasks | Cleaning-Inspektionen München Jan-Mai 2026 |
| **damage_cases.json** | 1.637 Plates · 6.773 Cases · 9.780 Damages | Administrativer Schadensregister-Auszug |
| **exterior_photos/** | 1.630 Plates · 16.906 Bilder | 10-Winkel-Aufnahmen pro Auto |
| **damage_photos/** | 1.405 Plates · 41.963 Bilder | Schaden-Close-ups (DETAIL/AREA/OVERVIEW) |

### Annotation-Pool (nach Quality-Filter)

- **500 stratifizierte Autos** + 3 Test-Autos = **503 Autos**
- **6.202 Exterior-Bilder** (8-Winkel-Aufnahmen, die Inferenz-Domäne)
- **2.941 DB-Damages** in den 500 Autos
- Davon nach Filterung (ohne Scheibe/Felge, nur vor Foto-Zeitpunkt):
  - **1.706 relevante Karosserie-Damages**
- Klassen-Verteilung: 68% scratch · 13% stone_chip · 12% dent · Rest <3% jeweils

### Tooling-Status
- Annotation-Tool live mit Konva-Canvas (Zoom/Pan)
- Multi-Model VLM-Vergleich (Gemini Pro 🔴 vs Flash 🔵)
- 3-Schicht Anti-Hallucination (Prompt → NMS → Cluster-Filter)
- Tile-Mode (3×3 Multi-Scale) + Standard
- Persistente Speicherung in SQLite

---

## COMPLICATION — Das Kernproblem

### Wir haben Bilder UND Damages — aber keinen Link dazwischen

```
┌────────────────────┐         ┌────────────────────┐
│  Exterior-Bilder   │         │  damage_cases.json │
│  6.202 Aufnahmen   │  ???    │  1.706 Damages     │
│  vom Auto-Winkel   │   ◄──►  │  mit Part/Side/    │
│                    │         │  Severity          │
└────────────────────┘         └────────────────────┘
       │                                  │
       └─────── KEINE BBOX-VERBINDUNG ────┘
```

**Konkret bedeutet das:**

| Problem | Konsequenz |
|---|---|
| DB-Damage sagt "Scratch · Door, front · DRIVER_SIDE" | Wir wissen nicht **WO im DIAGONAL_FRONT_LEFT-Foto** dieser Scratch ist (Pixel-Koordinaten) |
| DB-Damage existiert | aber ist evtl. **bereits repariert** vor dem Foto, oder **zu klein für 1280px-Resolution** |
| Foto zeigt **neuen Schaden** | aber DB hat ihn nicht (Customer-Damage seit letzter Erfassung) |
| Multiple Damages in einem Case | wir wissen nicht welcher zu welcher BBox gehört |

### Konsequenz für Vendor-Vergleich

Wir können aktuell NICHT objektiv messen:
- ❌ Welches Modell ist **präziser** (Precision)
- ❌ Welches Modell **übersieht weniger** (Recall)
- ❌ Wer hat **weniger False Positives**
- ❌ Statistisch belastbare Aussagen mit Konfidenzintervall

### Was wir können

- ✅ Qualitative Beobachtungen ("Pro denkt länger, Flash ist aggressiver")
- ✅ Latenz/Cost-Vergleich (objektive Metriken)
- ✅ Konsistenz zwischen Modellen (IoU-Overlap-Rate)
- ✅ Hallucination-Pattern-Detection (Cluster-Filter trigger rate)

→ **Für eine "Multi-Million-€"-Entscheidung reicht das nicht.**

---

## SOLUTION — 3.000 Bilder manuelle Annotation (Final)

### Setup-Überblick

```
┌──────────────────────────────────────────────────────────────────┐
│  TOTAL: 3.000 BILDER MANUELL ANNOTIERT                            │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│   ┌─────────────────────────────────────────────────────┐         │
│   │ GOLD TEST-SET — 300 Bilder                            │         │
│   │ Wer:     Sixt-Damage-Expert (höchste Qualität)        │         │
│   │ Zweck:   • Vendor-Vergleich (Gemini vs GPT vs Claude) │         │
│   │          • NIE für Training/Tuning verwendet         │         │
│   │          • Held-Out Reference-Truth                  │         │
│   │ Aufwand: ~25h × €100 = €2.500                          │         │
│   └─────────────────────────────────────────────────────┘         │
│                                                                    │
│   ┌─────────────────────────────────────────────────────┐         │
│   │ VAL/TRAIN — 2.700 Bilder                              │         │
│   │ Wer:     Sixt-internes Indien-Team                    │         │
│   │          (5-8 Annotatoren parallel)                  │         │
│   │ Zweck:   • Trainings-Daten für YOLO-Modell           │         │
│   │          • Validation-Set für Hyperparameter         │         │
│   │ Workflow: Mit Gemini Pre-Labels (3× schneller)       │         │
│   │ Aufwand: ~120-150h × €15 = €1.800-2.250              │         │
│   └─────────────────────────────────────────────────────┘         │
│                                                                    │
└──────────────────────────────────────────────────────────────────┘
```

### Stratifikation der 3.000 Bilder

```
View-Typen (10×):
  EXTERIOR_FRONT/REAR_STRAIGHT         300 × 2 = 600
  DIAGONAL_FRONT/REAR_LEFT/RIGHT       300 × 4 = 1.200
  TYRE_RIM_FRONT/REAR_LEFT/RIGHT       300 × 4 = 1.200
                                       ──────────────
                                       3.000

Damage-Klassen (proportional zur natürlichen Verteilung):
  scratch        ~1.800  (60%)
  stone_chip       ~330  (11%)  ← auf Karosserie nach Filter
  dent             ~330  (11%)
  crack             ~60  ( 2%)  ← oversampled für Class-Balance
  missing           ~60  ( 2%)
  major             ~30  ( 1%)
  other             ~30  ( 1%)
  "clean" (keine)  ~360  (12%)  ← wichtig für False-Positive-Test
                  ──────
                  3.000

Auto-Diversität:
  Mindestens 200 verschiedene Plates (von den 500 stratifizierten)
  → ~15 Bilder pro Auto im Schnitt
  → Vermeidet Overfit auf einzelne Autos
```

### Setup-Phasen (Empfohlener Ansatz: Hybrid)

```
┌───────────────────────────────────────────────────────────────────┐
│ Phase 1: DAMAGE-KATALOG (Sixt-Expert, 1-2 Wochen)                  │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │ Sixt-Damage-Expert(in) baut Annotation-Guideline:          │   │
│  │  • Visuelle Beispiele pro Klasse                           │   │
│  │  • Severity-Definitionen mit cm-Schwellen                  │   │
│  │  • Edge-Cases: Dreck vs Damage, Reflexion vs Scratch       │   │
│  │  • Sixt-spezifische Klassen-Abgrenzungen                   │   │
│  │  → HTML-Katalog "damage_catalog.html" mit ~50 Bildern      │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────────┐
│ Phase 2: GOLD-STANDARD-SET (Sixt-Expert, parallel)                 │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │ Sixt-Expert annotiert 200 zufällig stratifizierte Bilder   │   │
│  │ → Das "Goldene 200" — Reference-Truth                      │   │
│  │ Wird genutzt für:                                          │   │
│  │   • Annotator-Trainings-Bilder                             │   │
│  │   • QA-Honeypots (mixed in random)                         │   │
│  │   • Final Test-Set (30 davon nie sehen lassen)             │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────────┐
│ Phase 3: AI-PRE-ANNOTATION (Gemini, parallel laufend)              │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │ Gemini 3.1 Pro (Tile-Mode + CoT) auf alle 6.202 Bilder    │   │
│  │ Output pro Bild: BBox-Vorschläge mit Confidence            │   │
│  │ Confidence-Routing für Phase 4:                            │   │
│  │   HIGH (>0.85)   ~30%  → Annotator: nur bestätigen (~5s)   │   │
│  │   MEDIUM         ~50%  → Annotator: korrigieren (~25s)     │   │
│  │   LOW (<0.5)     ~20%  → Annotator: manuell from scratch   │   │
│  └────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────────┐
│ Phase 4: PRODUKTION-ANNOTATION (3-5 Wochen)                        │
│  Option A: Sixt-intern (3-5 Damage-Spezialisten)                   │
│  Option B: Externe (z.B. iMerit/Scale AI India)                    │
│  Option C: Hybrid                                                  │
│                                                                     │
│  Workflow je Bild im Annotation-Tool:                              │
│   1. Bild öffnen → Gemini Pre-Labels erscheinen                    │
│   2. Annotator: Accept (✓) · Reject (✕) · Edit BBox · Add new      │
│   3. Klasse + Severity bestätigen/anpassen                         │
│   4. Save → COCO-Format Export                                     │
│                                                                     │
│  Quality Gates:                                                    │
│   • 10% Bilder doppelt annotiert (zwei Annotatoren) → Cohen's κ    │
│   • Sixt-Expert spot-check 5% täglich                              │
│   • Target: κ ≥ 0.80, mIoU ≥ 0.75 vs Gold-Standard                 │
└───────────────────────────────────────────────────────────────────┘
                              ↓
┌───────────────────────────────────────────────────────────────────┐
│ Phase 5: VENDOR-VERGLEICH + MODEL-TRAINING                         │
│  • 4 Vendor-APIs auf TEST-Set (30 zurückgehaltene Bilder)          │
│  • mAP@50, Per-Class-Precision/Recall, statistische Tests          │
│  • Final-Modell: YOLOv11-seg auf eigenen Annotationen trainiert    │
└───────────────────────────────────────────────────────────────────┘
```

### Drei Optionen für Phase 4 — Pros & Cons

| Aspekt | A: Sixt-intern (Damage-Specialists) | B: Externe (z.B. iMerit India) | C: Hybrid (Lead intern, Volume extern) |
|---|---|---|---|
| **Domain-Knowledge** | ✅ Sehr hoch | ⚠️ Muss trainiert werden | ✅ Bestes von beiden |
| **Geschwindigkeit** | ❌ Langsam (Sixt-Personal limitiert) | ✅ Hoch (Parallel-Team) | ✅ Hoch |
| **Kosten** | €€ (interne Lohnkosten) | € (~€10-15/h externe) | €€ |
| **GDPR / Compliance** | ✅ Trivial | ⚠️ AVV nötig + Plate-Blurring | ⚠️ AVV nötig |
| **Qualität direkt** | ✅ Sehr hoch | ⚠️ Variabel, braucht QA-Loop | ✅ Hoch |
| **Skalierbarkeit** | ❌ Eng | ✅ Sehr gut | ✅ Sehr gut |
| **Setup-Aufwand** | Niedrig | Hoch (Vendor-Selection, Onboarding) | Mittel |
| **Liability bei Fehler** | Sixt selbst verantwortlich | externe Verantwortung möglich | gemischt |

### Meine konkrete Empfehlung: **Hybrid (Option C)**

**Begründung:**
- **Sixt-Damage-Expert(in)** als Annotation-Lead:
  - Baut Damage-Katalog
  - Annotiert die 200 Gold-Bilder
  - Reviewt täglich 5% der externen Annotationen
  - Definiert Edge-Case-Entscheidungen
  - **Aufwand: ~80h über 6 Wochen** (≈ 30% Auslastung)

- **Externe Annotatoren (5-8 Personen)** für die Volumenarbeit:
  - Bekommen Damage-Katalog + Gold-Beispiele
  - Arbeiten mit AI-Pre-Annotations (3-4× schneller)
  - 1-Wochen-Training mit Sixt-Expert
  - **Aufwand: ~150-200 Arbeitsstunden gesamt**

**Warum nicht rein intern?**
- Sixt-Damage-Experten sind teuer und zeitlich nicht skalierbar
- 6.000 Bilder = realistisch 4-6 Wochen Vollzeit-Arbeit für 1 Person
- Mit externem Team: 1-2 Wochen Wall-Time

**Warum nicht rein extern?**
- Domain-Wissen muss aufgebaut werden
- Qualitäts-Drift ohne interne Quality-Gates
- Sixt-spezifische Klassen-Definitionen externen Annotatoren fremd

### Damage-Katalog — was rein muss

```
damage_catalog/
├── README.md  (Annotation-Workflow + Guidelines)
├── classes/
│   ├── scratch/
│   │   ├── examples_clear.png   (10 eindeutige Beispiele)
│   │   ├── examples_borderline.png  (Grenzfälle)
│   │   ├── not_scratch.png  (Reflexionen, Dirt — was NICHT zählt)
│   │   └── severity_examples.png  (<3cm, 3-10cm, >10cm)
│   ├── stone_chip/  ...
│   ├── dent/  ...
│   ├── crack/  ...
│   ├── missing/  ...
│   ├── major/  ...
│   └── other/  ...
├── edge_cases.html  (Schwierige Entscheidungen mit Expert-Annotation)
├── bbox_guidelines.md  (Wie tight, was inkludieren, was nicht)
└── workflow.md  (Tool-Bedienung, Tastenkürzel, Save-Workflow)
```

### Kosten-Schätzung (3.000 Bilder)

| Position | Schätzung |
|---|---|
| Sixt-Damage-Expert (Damage-Katalog + 300 Gold + QA über 6 Wochen, ~50h × €100) | €5.000 |
| AI-Pre-Annotation Gemini Tile-Mode (3.000 Bilder × ~€0.25) | €750 |
| Indien-Team-Annotation (2.700 Bilder × ~30s × 5 Personen, €15/h) | €1.800 |
| QA-Overhead + Honeypot-Tasks (10% Overlap) | €1.000 |
| Tool-Anpassungen + Damage-Katalog-Build | €2.000 |
| **Total** | **~€10.500** |

→ Bei ~€3.50 pro Bild **inkl. Sixt-Expert-Lead** — vergleichbar mit Industrie-Standards (Scale AI: €5-30/BBox).

### Timeline

```
Woche 1:  ███░░░░░░░  Damage-Katalog + Gold-200 Annotation (Sixt-Expert)
Woche 2:  ████░░░░░░  AI-Pre-Annotation läuft parallel · Annotator-Training
Woche 3:  ██████░░░░  Volle Annotation (5-8 externe Annotatoren parallel)
Woche 4:  ██████░░░░  Annotation läuft + tägliche QA
Woche 5:  ██████░░░░  Annotation läuft + tägliche QA
Woche 6:  ████░░░░░░  Reconciliation + Final-QA
Woche 7:  ██░░░░░░░░  Vendor-Vergleich auf Test-Set
Woche 8:  ██████████  YOLOv11-seg Training startet
```

---

## Open Questions (du musst entscheiden)

1. **Sixt-interne Damage-Experten verfügbar?** Wenn ja: wie viele Stunden über 6 Wochen?
2. **Externe Annotation-Vendor bevorzugt?** iMerit, Scale, Labelbox haben unterschiedliche Qualitäts/Preis-Profile
3. **GDPR-Plate-Blurring** vor externem Annotation-Versand?
4. **Annotation-Target genau:** Wir haben 6.202 Bilder. Müssen alle annotiert werden, oder reichen 2-3k für Training + Test?
5. **Polygone oder BBoxes?** Polygone besser für unregelmäßige Schäden (Kratzer), aber 3× langsamer

### Mein Bauchgefühl als ML-Engineer

- **Start mit 2.000 Bildern annotieren** (stratifiziert über die 7 Klassen)
- **Train YOLOv11** auf diesem Set
- **Active Learning Loop**: YOLO findet "unsichere" Bilder → diese werden als nächstes annotiert
- **Erweitern auf 5.000-6.000** falls nötig (wenn Validation-mAP plateauiert)

Das ist effizienter als alles auf einmal — du investierst nur in Annotation wenn das Modell sie wirklich braucht.

### Nächster Schritt (wenn du diesen Plan freigibst)

1. Ich baue den **Damage-Katalog-Skeleton** (HTML-Template, Sixt-Expert füllt Beispiele rein)
2. Ich baue den **Manual-Annotation-Modus** ins Tool (BBox/Polygon zeichnen, Klasse wählen)
3. Wir machen mit **dir** einen Pilot auf 20 Bildern → kalibrieren das Tool
4. Dann skaliert
