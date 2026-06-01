import os
import requests
import json
from datetime import datetime, timedelta
import time

# Sixt-Identity-JWT, ~5 Min Lebensdauer. Via env-var injizieren:
#   export SIXT_LYNX_TOKEN="$(<token-aus-browser-dev-tools>)"
TOKEN = os.environ.get("SIXT_LYNX_TOKEN", "")
if not TOKEN:
    raise SystemExit("SIXT_LYNX_TOKEN env-var nicht gesetzt — Token aus Lynx-Browser kopieren")

# Echter gRPC-Endpoint (nicht die Browser-UI)
URL = "https://grpc-query-tool-prod.orange.sixt.com/com.sixt.service.vehicle_tasks_management.api.VehicleTasksManagement/GetTaskPhotosByBranch"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://lynx.orange.sixt.com",
}

def fetch_chunk(branch_id, start: datetime, end: datetime):
    payload = {
        "branch_id": str(branch_id),
        "task_type": "CLEANLINESS_PHOTOS",
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_time":   end.strftime("%Y-%m-%dT%H:%M:%S")
    }
    response = requests.post(URL, json=payload, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def fetch_all(branch_id, total_start, total_end, chunk_hours=6):
    all_photos = []
    current = total_start

    while current < total_end:
        chunk_end = min(current + timedelta(hours=chunk_hours), total_end)
        print(f"Fetching {current} → {chunk_end} ...")
        
        try:
            result = fetch_chunk(branch_id, current, chunk_end)
            photos = result.get("vehicle_photos", [])
            all_photos.extend(photos)
            print(f"  ✓ {len(photos)} Fahrzeuge (gesamt: {len(all_photos)})")
        except Exception as e:
            print(f"  ✗ Fehler: {e}")
        
        current = chunk_end
        time.sleep(0.3)

    return all_photos

# --- Start ---
if __name__ == "__main__":
    results = fetch_all(
        branch_id=11,
        total_start=datetime(2026, 1, 1),
        total_end=datetime(2026, 5, 1),
        chunk_hours=6  # ~250 Ergebnisse pro Chunk
    )

    with open("photos_export.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n✅ Fertig: {len(results)} Einträge → photos_export.json")