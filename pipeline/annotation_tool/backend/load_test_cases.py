"""Lädt die 3 Test-Autos (BMW, Ford, VW) in die Annotation-Tool-DB.
24 Bilder gesamt — keine DB-Damages (User-Aufnahmen für Live-Test).
"""
import sqlite3
import sys
from pathlib import Path
from PIL import Image

DB = Path(__file__).parent / "data" / "annotations.db"
TEST_DIR = Path(__file__).parent.parent.parent.parent / "Test cases Damage detection 2"


def add_is_test_column(conn):
    """Add is_test column if missing."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cars)").fetchall()]
    if "is_test" not in cols:
        conn.execute("ALTER TABLE cars ADD COLUMN is_test INTEGER DEFAULT 0")
        conn.commit()
        print("  + is_test column added")


def load():
    if not DB.exists():
        print(f"❌ DB nicht da: {DB}")
        sys.exit(1)
    if not TEST_DIR.exists():
        print(f"❌ Test-Ordner nicht da: {TEST_DIR}")
        sys.exit(1)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    add_is_test_column(conn)

    for brand_dir in sorted(TEST_DIR.iterdir()):
        if not brand_dir.is_dir():
            continue
        brand = brand_dir.name
        plate_safe = f"TEST_{brand}"
        plate_original = f"TEST · {brand}"

        # Check if already loaded
        existing = conn.execute("SELECT plate_safe FROM cars WHERE plate_safe = ?", (plate_safe,)).fetchone()
        if existing:
            # Delete + re-insert (clean slate)
            conn.execute("DELETE FROM images WHERE plate_safe = ?", (plate_safe,))
            conn.execute("DELETE FROM db_damages WHERE plate_safe = ?", (plate_safe,))
            conn.execute("DELETE FROM cars WHERE plate_safe = ?", (plate_safe,))

        imgs = sorted(brand_dir.glob("*.jpg"))
        n_views = len(imgs)
        conn.execute(
            "INSERT INTO cars (plate_safe, plate_original, car_score, n_unique_views, n_damages, damage_classes, is_test) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (plate_safe, plate_original, 100.0, n_views, 0, "test_case")
        )

        for img_path in imgs:
            try:
                with Image.open(img_path) as im:
                    w, h = im.size
            except Exception as e:
                print(f"  ⚠️ {img_path.name}: {e}")
                w = h = 0
            view = f"TEST_{img_path.stem}"  # z.B. TEST_1087
            conn.execute(
                "INSERT INTO images (plate_safe, view, path, width, height, quality_score, is_hard_fail) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (plate_safe, view, str(img_path.resolve()), w, h, 100.0)
            )

        print(f"  ✓ {brand}: {n_views} Bilder geladen")

    conn.commit()
    n_test = conn.execute("SELECT COUNT(*) FROM cars WHERE is_test = 1").fetchone()[0]
    n_test_imgs = conn.execute("SELECT COUNT(*) FROM images i JOIN cars c ON i.plate_safe = c.plate_safe WHERE c.is_test = 1").fetchone()[0]
    print(f"\n✅ Test-Cases in DB: {n_test} Autos, {n_test_imgs} Bilder")
    conn.close()


if __name__ == "__main__":
    load()
