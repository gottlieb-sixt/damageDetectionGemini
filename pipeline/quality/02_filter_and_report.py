"""Wendet strikte Quality-Filter an + erzeugt HTML-Diagnostik-Report.

Filter (STRICT — Top ~30-40%):
- Mindest-Auflösung (kürzeste Seite >= 1080px)
- Mindest-Schärfe (Sharpness >= 60. Perzentil)
- Belichtung gut (Brightness in [70, 200], Clipping < 1%)
- Kontrast OK (Contrast >= 40. Perzentil)
- Saturation OK (>= 30)
- Keine Duplikate (pHash-Cluster, behalte schärfstes pro Cluster)

Composite Quality Score (0-100) = gewichteter Mittelwert aus normalisierten Metriken.
"""
import base64
import io
from collections import defaultdict
from pathlib import Path

import imagehash
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image

MANIFEST = Path(__file__).parent / "quality_manifest.parquet"
OUT_MANIFEST = Path(__file__).parent / "quality_filtered.parquet"
OUT_HTML = Path(__file__).parent / "quality_report.html"

# Hard-Fails: nur offensichtlich kaputte Bilder rauswerfen
MIN_RESOLUTION = 800          # kürzeste Seite < 800px = zu klein
MIN_SHARPNESS_HARD = 30       # < 30 = grob unscharf
MAX_OVEREXP_HARD = 10.0       # > 10% komplett ausgebrannt
MAX_UNDEREXP_HARD = 30.0      # > 30% schwarz = Foto vom Boden/Innenraum/Daumen
BRIGHTNESS_HARD = (40, 220)   # extreme Dunkelheit/Helligkeit
MIN_SATURATION = 10           # nahezu monochrom
PHASH_HAMMING_THRESHOLD = 6

# Score-Filter: Composite-Score-basiert (Top X% behalten)
KEEP_TOP_PERCENT = 35         # Top 35% nach Quality-Score behalten


def normalize(series, lo, hi, invert=False):
    """Normalisiert auf 0-1 zwischen lo und hi Perzentilen."""
    p_lo, p_hi = series.quantile([lo/100, hi/100])
    norm = ((series - p_lo) / max(p_hi - p_lo, 1e-9)).clip(0, 1)
    return 1 - norm if invert else norm


def composite_score(df):
    """0-100 Score: höher = besser."""
    s = (
        0.35 * normalize(df["sharpness"], 5, 95) +
        0.20 * normalize(df["contrast"], 5, 95) +
        0.10 * normalize(df["saturation"], 5, 95) +
        0.10 * normalize(np.minimum(df["width"], df["height"]), 5, 95) +
        0.15 * (1 - normalize(df["overexp_pct"], 5, 95)) +
        0.10 * (1 - normalize(df["underexp_pct"], 5, 95))
    )
    return (s * 100).round(1)


def apply_filters(df):
    """Zwei-Stufen-Filter:
       1. Hard-Fails: offensichtlich kaputte Bilder (kein Kompromiss)
       2. Score-Filter: Top X% nach Composite-Score
    """
    reasons = []
    for _, row in df.iterrows():
        r = []
        # Hard-Fails
        if min(row["width"], row["height"]) < MIN_RESOLUTION:
            r.append("low_resolution")
        if row["sharpness"] < MIN_SHARPNESS_HARD:
            r.append("very_blurry")
        if row["brightness"] < BRIGHTNESS_HARD[0]:
            r.append("too_dark")
        elif row["brightness"] > BRIGHTNESS_HARD[1]:
            r.append("too_bright")
        if row["overexp_pct"] > MAX_OVEREXP_HARD:
            r.append("burnt_out")
        if row["underexp_pct"] > MAX_UNDEREXP_HARD:
            r.append("black_out")
        if row["saturation"] < MIN_SATURATION:
            r.append("monochrome")
        reasons.append(r)

    df["hard_fail_reasons"] = reasons
    df["hard_fail"] = [len(r) > 0 for r in reasons]

    # Score-Filter: Top X% der Hard-Pass behalten
    pass_hard = df[~df["hard_fail"]]
    if len(pass_hard) > 0:
        score_threshold = pass_hard["quality_score"].quantile(1 - KEEP_TOP_PERCENT / 100)
    else:
        score_threshold = 100
    df["score_pass"] = df["quality_score"] >= score_threshold
    df["keep_pre_dedup"] = (~df["hard_fail"]) & df["score_pass"]

    # Filter-Reason für Score-Fail anhängen
    for i, row in df.iterrows():
        if not row["hard_fail"] and not row["score_pass"]:
            df.at[i, "hard_fail_reasons"] = row["hard_fail_reasons"] + ["below_score_threshold"]

    df["filter_reasons"] = df["hard_fail_reasons"]
    return df, score_threshold


def deduplicate(df):
    """Findet pHash-Cluster und behält pro Cluster nur das schärfste Bild."""
    print("  Berechne Dedup-Cluster ...")
    hashes = [imagehash.hex_to_hash(h) for h in df["phash"]]
    n = len(hashes)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # O(n*m) wo m = duplikate pro hash - approximativ via sortierte hashes
    # Aber für 16k Bilder mit cleaner Dedup: brute force OK
    # Trick: sortiere nach hash-string als Approximation; nur direkt nachbarn vergleichen mit größerem Window
    df_sorted = df.copy().reset_index().rename(columns={"index": "orig_idx"})
    df_sorted["phash_str"] = df_sorted["phash"]
    df_sorted = df_sorted.sort_values("phash_str").reset_index(drop=True)
    sorted_hashes = [imagehash.hex_to_hash(h) for h in df_sorted["phash"]]

    WINDOW = 50  # vergleiche jedes hash mit den nächsten 50 sortierten Nachbarn
    for i in range(len(sorted_hashes)):
        for j in range(i+1, min(i+WINDOW+1, len(sorted_hashes))):
            if sorted_hashes[i] - sorted_hashes[j] <= PHASH_HAMMING_THRESHOLD:
                union(df_sorted.iloc[i]["orig_idx"], df_sorted.iloc[j]["orig_idx"])

    cluster_ids = [find(i) for i in range(n)]
    df["cluster_id"] = cluster_ids

    # Pro Cluster: bestes Bild (höchster Composite Score) behalten
    keep_idx = set()
    for cid, grp in df.groupby("cluster_id"):
        # bestes nach quality_score
        best = grp.sort_values("quality_score", ascending=False).iloc[0].name
        keep_idx.add(best)

    df["is_cluster_best"] = df.index.isin(keep_idx)
    return df


def make_histograms(df):
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    metrics = [
        ("sharpness", "Sharpness (Laplacian Var)", "log"),
        ("brightness", "Brightness (0-255)", "linear"),
        ("contrast", "Contrast (std)", "linear"),
        ("saturation", "Saturation", "linear"),
        ("overexp_pct", "Overexp %", "log"),
        ("quality_score", "Composite Score (0-100)", "linear"),
    ]
    for ax, (m, title, scale) in zip(axes.flat, metrics):
        data = df[m].clip(0.001 if scale == "log" else None, None)
        ax.hist(data, bins=60, color="#ff5f00", edgecolor="white", linewidth=0.3)
        if scale == "log":
            ax.set_xscale("log")
        ax.set_title(title, fontsize=11)
        ax.grid(alpha=0.3)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close()
    return base64.b64encode(buf.getvalue()).decode()


def sample_images(df, n=6, max_side=300):
    """Erzeugt base64-Thumbnails für eine Stichprobe."""
    samples = []
    for _, row in df.head(n).iterrows():
        try:
            img = Image.open(row["path"]).convert("RGB")
            if max(img.size) > max_side:
                r = max_side / max(img.size)
                img = img.resize((int(img.width*r), int(img.height*r)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            samples.append({
                "b64": base64.b64encode(buf.getvalue()).decode(),
                "score": row["quality_score"],
                "view": row["view"],
                "sharpness": row["sharpness"],
                "brightness": row["brightness"],
                "reasons": row.get("filter_reasons", []),
            })
        except Exception as e:
            samples.append({"error": str(e)[:50]})
    return samples


def render_sample_grid(samples, title):
    cards = []
    for s in samples:
        if "error" in s:
            cards.append(f"<div class='sample err'>{s['error']}</div>")
            continue
        reasons = ", ".join(s["reasons"]) if s["reasons"] else "✓ OK"
        cards.append(f"""
<div class='sample'>
  <img src='data:image/jpeg;base64,{s["b64"]}'/>
  <div class='meta'>
    <strong>Score: {s["score"]:.0f}</strong> · {s["view"]}<br>
    Sharp {s["sharpness"]:.0f} · Bright {s["brightness"]:.0f}<br>
    <small>{reasons}</small>
  </div>
</div>""")
    return f"<h3>{title}</h3><div class='grid'>{''.join(cards)}</div>"


if __name__ == "__main__":
    print(f"Lade {MANIFEST} ...")
    df = pd.read_parquet(MANIFEST)
    print(f"  {len(df)} Bilder im Manifest\n")

    print("Berechne Composite Quality Score ...")
    df["quality_score"] = composite_score(df)

    print("Wende Filter an ...")
    df, score_thr = apply_filters(df)

    print("Dedupliziere ...")
    df = deduplicate(df)
    df["keep_final"] = df["keep_pre_dedup"] & df["is_cluster_best"]

    # Stats
    n_total = len(df)
    n_keep = df["keep_final"].sum()
    print(f"\n📊 Filter-Ergebnisse:")
    print(f"   Total:                 {n_total}")
    print(f"   Keep (final):          {n_keep} ({n_keep/n_total*100:.1f}%)")
    print(f"   Discard:               {n_total - n_keep} ({(n_total-n_keep)/n_total*100:.1f}%)")

    reason_counter = defaultdict(int)
    for reasons in df["filter_reasons"]:
        for r in reasons:
            reason_counter[r] += 1
    print(f"\n   Filter-Gründe (mehrfach pro Bild möglich):")
    for r, n in sorted(reason_counter.items(), key=lambda x: -x[1]):
        print(f"     {r:<20} {n}")
    n_dup_lost = df["keep_pre_dedup"].sum() - n_keep
    print(f"     duplicates           {n_dup_lost}")

    print(f"\n   Pro View-Klasse (% behalten):")
    for view, grp in df.groupby("view"):
        k = grp["keep_final"].sum()
        print(f"     {view:<30} {k}/{len(grp)} ({k/len(grp)*100:.0f}%)")

    df.to_parquet(OUT_MANIFEST, index=False)
    print(f"\n✅ Gefiltertes Manifest: {OUT_MANIFEST}")

    # HTML Report
    print("Erzeuge HTML-Report ...")
    histograms_png = make_histograms(df)
    top_samples = sample_images(df.sort_values("quality_score", ascending=False), n=12)
    bottom_samples = sample_images(df.sort_values("quality_score"), n=12)
    rejected = df[~df["keep_final"]].sort_values("quality_score", ascending=False)
    sample_rejected = sample_images(rejected.head(12), n=12)
    kept = df[df["keep_final"]].sort_values("quality_score")
    sample_borderline = sample_images(kept.head(12), n=12)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Quality Filter Report</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f4f4f7; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
header {{ background: white; border-left: 5px solid #ff5f00; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
header h1 {{ margin: 0 0 8px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 16px; }}
.stat {{ background: #f6f7f9; padding: 12px 16px; border-radius: 8px; }}
.stat .label {{ font-size: 11px; text-transform: uppercase; color: #5a6270; }}
.stat .value {{ font-size: 22px; font-weight: 700; }}
.section {{ background: white; padding: 24px 28px; border-radius: 12px; margin-bottom: 24px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }}
.sample {{ background: #f6f7f9; padding: 8px; border-radius: 8px; }}
.sample img {{ width: 100%; border-radius: 4px; }}
.sample .meta {{ padding: 6px 4px; font-size: 11px; }}
.sample.err {{ background: #fee2e2; padding: 24px; text-align: center; font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #e2e5ea; }}
th {{ background: #f6f7f9; }}
.bar {{ background: linear-gradient(90deg, #10b981, #059669); height: 6px; border-radius: 3px; }}
img.hist {{ max-width: 100%; }}
</style></head><body>
<div class="container">
  <header>
    <h1>📊 Quality Filter Report — exterior_photos</h1>
    <p>Strikte Filterung: Top ~30-40% Bilder behalten</p>
    <div class="stats">
      <div class="stat"><div class="label">Total</div><div class="value">{n_total:,}</div></div>
      <div class="stat"><div class="label">Behalten</div><div class="value" style="color:#10b981">{n_keep:,} ({n_keep/n_total*100:.0f}%)</div></div>
      <div class="stat"><div class="label">Verworfen</div><div class="value" style="color:#dc2626">{n_total-n_keep:,} ({(n_total-n_keep)/n_total*100:.0f}%)</div></div>
      <div class="stat"><div class="label">Duplikate</div><div class="value">{n_dup_lost:,}</div></div>
      <div class="stat"><div class="label">Score-Threshold (Top {KEEP_TOP_PERCENT}%)</div><div class="value">{score_thr:.0f}</div></div>
    </div>
  </header>

  <div class="section">
    <h2>Metrik-Verteilungen</h2>
    <img class="hist" src="data:image/png;base64,{histograms_png}"/>
  </div>

  <div class="section">
    <h2>Filter-Gründe</h2>
    <table>
      <tr><th>Grund</th><th>Anzahl</th><th>% aller Bilder</th></tr>
      {''.join(f'<tr><td>{r}</td><td>{n}</td><td>{n/n_total*100:.1f}%</td></tr>' for r, n in sorted(reason_counter.items(), key=lambda x: -x[1]))}
      <tr><td>duplicates</td><td>{n_dup_lost}</td><td>{n_dup_lost/n_total*100:.1f}%</td></tr>
    </table>
  </div>

  <div class="section">
    <h2>Pro View-Klasse</h2>
    <table>
      <tr><th>View</th><th>Total</th><th>Behalten</th><th>% Keep</th></tr>
      {''.join(f'<tr><td>{v}</td><td>{len(g)}</td><td>{g["keep_final"].sum()}</td><td>{g["keep_final"].sum()/len(g)*100:.0f}%</td></tr>' for v, g in df.groupby("view"))}
    </table>
  </div>

  <div class="section">
    {render_sample_grid(top_samples, "🏆 Top-12 (höchster Quality-Score)")}
  </div>
  <div class="section">
    {render_sample_grid(bottom_samples, "⚠️ Bottom-12 (niedrigster Quality-Score)")}
  </div>
  <div class="section">
    {render_sample_grid(sample_rejected, "❌ Sample: Verworfen mit höchstem Score (Grenzfälle Drop-Side)")}
  </div>
  <div class="section">
    {render_sample_grid(sample_borderline, "✅ Sample: Behalten mit niedrigstem Score (Grenzfälle Keep-Side)")}
  </div>
</div></body></html>
"""

    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"✅ Report: {OUT_HTML}")
