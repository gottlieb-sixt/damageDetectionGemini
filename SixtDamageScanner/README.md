# Sixt Damage Scanner — Android (Kotlin)

Native Android-App für das Samsung A33 (oder jedes andere Android-8+-Gerät).
Nimmt 12 standardisierte Auto-Winkel auf, schickt jedes Bild **direkt** an die
Sixt LLM-Gateway (Gemini 3.1 Pro / 3.5 Flash, optional 3×3 Tile-Mode), zeigt
am Ende alle Fotos mit Schadens-BBoxes und persistiert Session-Logs lokal.

**Kein Backend nötig** — die App spricht direkt mit `llm.orange.sixt.com`.

Volle Architektur-Übersicht siehe [../PROJECT.md](../PROJECT.md).

---

## Voraussetzungen

- macOS / Linux / Windows mit installiertem Android SDK
- **JDK 17 oder 21** (Android Studio bringt eines mit unter `Contents/jbr`)
- **adb** unter `~/Library/Android/sdk/platform-tools/adb` (macOS)
- Samsung A33 mit aktiviertem **USB-Debugging**

## 1) APK bauen

```bash
cd SixtDamageScanner

# JAVA_HOME setzen wenn nötig
JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
  ./gradlew assembleDebug
```

Falls `SDK location not found`, lege `local.properties` an:

```bash
echo "sdk.dir=$HOME/Library/Android/sdk" > local.properties
```

## 2) Installieren

```bash
adb=$HOME/Library/Android/sdk/platform-tools/adb
$adb devices                                      # A33 muss "device" sein
$adb install -r app/build/outputs/apk/debug/app-debug.apk
$adb shell am start -n com.sixt.damagescanner/.MainActivity
```

## 3) App benutzen

App startet direkt im Capture-Screen mit Live-Kamera.

**Top-Pills (live umschaltbar):**

| Pill | Funktion |
|---|---|
| `[Pro] [Flash]` | Modell wählen |
| `[📐 1280 / 2048 / Max]` | Resize-Cap vor API-Send |
| `[Tile 3×3]` | 9 parallele Calls mit NMS + Cluster-Filter |
| `[↻]` | Reset (finalisiert laufende Session) |

**Flow:**

1. Auslöser drücken → Foto-Review erscheint sofort über der Live-Preview
2. `[↻ Wiederholen]` oder `[Weiter →]`
3. 12× durch alle Winkel
4. `[Auswertung →]` → Liste mit BBox-Overlays + Logs-Pfad

**API-Key:** als Default hardcoded in [ScanViewModel.kt:50](app/src/main/java/com/sixt/damagescanner/ScanViewModel.kt#L50) — kann in Settings (`⚙️`) überschrieben werden.

## 4) Session-Logs holen

```bash
adb=$HOME/Library/Android/sdk/platform-tools/adb
$adb pull /sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/ ./scanner-export/

# Aggregate per Session
for d in scanner-export/*/; do
  jq -r '.aggregates | "\(.n_photos)f \(.n_damages_total)d $\(.total_usd_estimated) \(.avg_latency_s)s"' "$d/session.json"
done
```

Logs enthalten pro Foto: Bytes-Up/Down, Latenz, Tokens, USD-Schätzung, alle BBoxes mit
Confidence + Panel + Reasoning, sowie das Original-JPEG. Details siehe
[PROJECT.md → Session-Logging](../PROJECT.md#session-logging--wie--wo).

---

## Projekt-Struktur

```text
SixtDamageScanner/
├── build.gradle.kts                       # AGP 8.9.1 + Kotlin 2.1.20
├── settings.gradle.kts
├── gradle.properties
├── gradle/wrapper/
├── gradlew, gradlew.bat
└── app/
    ├── build.gradle.kts                   # Compose, CameraX, OkHttp, Coil
    └── src/main/
        ├── AndroidManifest.xml
        ├── res/
        │   ├── drawable/ic_launcher_foreground.xml
        │   ├── mipmap-anydpi-v26/ic_launcher{,_round}.xml
        │   └── values/{themes,strings,ic_launcher_background}.xml
        └── java/com/sixt/damagescanner/
            ├── MainActivity.kt            # 3 Screens (Capture / Results / Settings)
            ├── ScanViewModel.kt           # UiState + Coroutines + Logger-Lifecycle
            ├── llm/
            │   ├── DamageAnalyzer.kt      # Tile-Splitter + NMS + Cluster-Filter
            │   ├── LlmGatewayClient.kt    # OkHttp + Telemetry
            │   └── DamagePrompt.kt        # CoT-Prompt + 12 View-Descriptions
            └── logging/
                ├── SessionLogger.kt       # Datei-basierte Session-Persistenz
                ├── Telemetry.kt           # Data classes + org.json-Serialisierung
                └── Pricing.kt             # Token → USD
```

---

## Troubleshooting

**"adb devices" leer** — USB-Debugging im Handy nicht freigegeben:
Einstellungen → Über das Telefon → Build-Nr. 7× tippen → Entwickleroptionen → USB-Debugging.

**adb sagt `device unauthorized`** — Handy entsperren, RSA-Popup bestätigen ("Immer zulassen").

**Build hängt bei `Configure project :app`** — erstmaliger Gradle-Download (~150 MB). Geduld.

**`compileSdk = 36` nicht gefunden** — Android-SDK aktualisieren:

```bash
$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager "platforms;android-36" "build-tools;35.0.0"
```

**App startet, Kamera schwarz** — Kamera-Berechtigung beim ersten Start "Erlauben".

**API-Calls schlagen fehl mit 401** — API-Key ungültig. Settings (`⚙️`) → neuen Key eintragen.
