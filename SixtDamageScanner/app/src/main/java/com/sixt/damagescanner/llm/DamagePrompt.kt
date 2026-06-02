package com.sixt.damagescanner.llm

object DamagePrompt {

    val VIEW_DESC: Map<String, String> = mapOf(
        "FRONT_STRAIGHT" to "Front straight-on view",
        "REAR_STRAIGHT" to "Rear straight-on view",
        "SIDE_LEFT" to "Left side view",
        "SIDE_RIGHT" to "Right side view",
        "DIAGONAL_FRONT_LEFT" to "Front-left diagonal",
        "DIAGONAL_FRONT_RIGHT" to "Front-right diagonal",
        "DIAGONAL_REAR_LEFT" to "Rear-left diagonal",
        "DIAGONAL_REAR_RIGHT" to "Rear-right diagonal",
        "TYRE_FRONT_LEFT" to "Front-left wheel close-up",
        "TYRE_FRONT_RIGHT" to "Front-right wheel close-up",
        "TYRE_REAR_LEFT" to "Rear-left wheel close-up",
        "TYRE_REAR_RIGHT" to "Rear-right wheel close-up",
    )

    fun buildCot(view: String): String {
        val desc = VIEW_DESC[view] ?: view
        return """You are a CAREFUL Sixt vehicle damage inspector examining a rental car photo.
The photo shows: $desc.

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
- **SPLASH DIRT / MUD SPATTER / ROAD GRIME**: brown/gray/black spatter patterns — especially on:
  * Wheels, rims, tires (very common — almost every car has this)
  * Rear bumper, rear fenders (kicked up while driving)
  * Lower side skirts and rocker panels
  * Behind the wheels (mud trails)
  These look like clusters of small dark spots/streaks. They are DIRT, NOT stone chips, NOT scratches.
  If you see a cluster of brown/dark spots on or near a wheel/rim/lower bumper, it's road dirt. SKIP IT.
- **General road salt residue / dust film**: matte gray-white deposits on lower body. Cosmetic dirt, not damage.

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
{
  "panels_scanned": ["front bumper", "hood"],
  "damages": [
    {
      "bbox_2d": [ymin, xmin, ymax, xmax],
      "label": "scratch",
      "confidence": 0.85,
      "severity": "light|medium|severe",
      "panel": "driver_door",
      "reasoning": "8cm diagonal scratch on driver door with visible paint disruption — verified NOT a reflection because the line is darker than surrounding paint"
    }
  ]
}

bbox_2d MUST be in 0-1000 normalized [ymin, xmin, ymax, xmax].
If no real damages visible, return {"panels_scanned": [...], "damages": []}.
QUALITY > QUANTITY. Fewer, high-confidence detections are better than many false positives."""
    }

    fun tileSuffix(view: String): String {
        val desc = VIEW_DESC[view] ?: view
        return "\n\nNOTE: This is a ZOOMED-IN TILE of a larger car photo showing the $desc. The car may only be partially visible in this tile. Detect damages within this tile only."
    }
}
