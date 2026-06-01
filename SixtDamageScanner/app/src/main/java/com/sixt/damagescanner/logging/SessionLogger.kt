package com.sixt.damagescanner.logging

import android.content.Context
import android.os.Build
import com.sixt.damagescanner.llm.LlmGatewayClient
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID

/**
 * Persists one scan session as a folder under app-specific external storage:
 *   /sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/<session_id>/
 *
 * Layout:
 *   session.json   — Session-Level Metadaten + Aggregate (final beim finalize())
 *   photos.json    — Liste aller Photo-Telemetrien (inkrementell, nach jedem Foto)
 *   NN_<VIEW>.jpg  — Original-Kamera-JPEGs
 *
 * Single-instance-per-VM. Thread-safety via synchronized blocks (records are
 * called from coroutines that may overlap in tile-mode after retake).
 */
class SessionLogger(private val appContext: Context) {

    private val rootDir: File by lazy {
        // getExternalFilesDir() returns /sdcard/Android/data/<pkg>/files/<sub>
        // No permission needed on Android 4.4+; pullable via `adb pull`.
        val external = appContext.getExternalFilesDir("SixtScanner")
            ?: File(appContext.filesDir, "SixtScanner")
        external.apply { mkdirs() }
    }

    private var sessionDir: File? = null
    private var sessionId: String? = null
    private var startedAtMs: Long = 0L
    private var modelKey: String = ""        // "flash" | "gemini"
    private var modelId: String = ""         // "vertex_ai/gemini-3.5-flash"
    private var tileMode: Boolean = false
    private var plate: String = ""
    private val photos: MutableMap<String, PhotoTelemetry> = linkedMapOf()  // keyed by view

    @Synchronized
    fun start(model: String, tileMode: Boolean, plate: String): String {
        // If a previous session is still open, finalize it best-effort
        if (sessionDir != null) finalize(reason = "auto_close_on_start")

        startedAtMs = System.currentTimeMillis()
        this.modelKey = model
        this.modelId = Pricing.modelIdFor(model)
        this.tileMode = tileMode
        this.plate = plate
        val plateSlug = plate.ifBlank { "no-plate" }
            .replace(Regex("[^A-Za-z0-9-]"), "")
            .ifBlank { "no-plate" }
        val ts = TS_FOLDER.format(Date(startedAtMs))
        val shortId = UUID.randomUUID().toString().take(6)
        val sid = "${ts}_${plateSlug}_$shortId"
        sessionId = sid
        sessionDir = File(rootDir, sid).apply { mkdirs() }
        photos.clear()
        writePhotosJson()
        return sid
    }

    @Synchronized
    fun recordPhoto(
        view: String,
        idx: Int,
        originalCacheFile: File,
        result: LlmGatewayClient.Result,
        capturedAtMs: Long,
        maxSideSetting: Int,
    ) {
        val dir = sessionDir ?: return
        val targetName = "%02d_%s.jpg".format(idx + 1, view)
        val targetFile = File(dir, targetName)
        try {
            if (originalCacheFile.exists()) originalCacheFile.copyTo(targetFile, overwrite = true)
        } catch (_: Exception) { /* keep going — file copy is best-effort */ }

        val telemetry = PhotoTelemetry(
            view = view,
            idx = idx,
            capturedAtIso = TS_ISO.format(Date(capturedAtMs)),
            originalFileName = targetName,
            originalSizeBytes = targetFile.length(),
            originalResolution = result.originalResolution,
            sentResolution = result.sentResolution,
            model = modelKey,
            modelId = modelId,
            tileMode = tileMode,
            maxSideSetting = maxSideSetting,
            calls = result.calls,
            nPreNms = result.nPreNms,
            nPostNms = result.nPostNms,
            nAfterClusterFilter = result.damages.size,
            nReflectionClusters = result.nReflectionClusters,
            finalDamages = result.damages,
        )
        photos[view] = telemetry
        writePhotosJson()
    }

    @Synchronized
    fun removePhoto(view: String) {
        val dir = sessionDir ?: return
        photos.remove(view)
        // Clean up the file too if present
        dir.listFiles()?.firstOrNull { it.name.endsWith("_$view.jpg") }?.delete()
        writePhotosJson()
    }

    @Synchronized
    fun finalize(reason: String = "normal"): File? {
        val dir = sessionDir ?: return null
        val ended = System.currentTimeMillis()
        val photoList = photos.values.toList()
        val totalBytesSent = photoList.sumOf { it.totalBytesSent }
        val totalBytesReceived = photoList.sumOf { it.totalBytesReceived }
        val totalPromptTokens = photoList.sumOf { it.totalPromptTokens }
        val totalCompletionTokens = photoList.sumOf { it.totalCompletionTokens }
        val totalUsd = photoList.sumOf { it.totalUsd }
        val latencies = photoList.map { it.totalLatencyS }
        val avgLatency = if (latencies.isNotEmpty()) latencies.average() else 0.0
        val p95Latency = latencies.sorted().let {
            if (it.isEmpty()) 0.0 else it[(it.size * 0.95).toInt().coerceAtMost(it.size - 1)]
        }

        val session = JSONObject().apply {
            put("session_id", sessionId)
            put("started_at", TS_ISO.format(Date(startedAtMs)))
            put("ended_at", TS_ISO.format(Date(ended)))
            put("duration_s", (ended - startedAtMs) / 1000.0)
            put("plate", plate)
            put("model", modelKey)
            put("model_id", modelId)
            put("tile_mode", tileMode)
            put("app_version", "0.1.0")
            put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("android_sdk", Build.VERSION.SDK_INT)
            put("close_reason", reason)
            put("aggregates", JSONObject().apply {
                put("n_photos", photoList.size)
                put("n_damages_total", photoList.sumOf { it.finalDamages.size })
                put("n_errors", photoList.count { it.hadError })
                put("total_bytes_sent", totalBytesSent)
                put("total_bytes_received", totalBytesReceived)
                put("total_prompt_tokens", totalPromptTokens)
                put("total_completion_tokens", totalCompletionTokens)
                put("total_usd_estimated", totalUsd)
                put("avg_latency_s", avgLatency)
                put("p95_latency_s", p95Latency)
            })
        }
        val sessionFile = File(dir, "session.json")
        sessionFile.writeText(session.toString(2))

        // Reset state — keep dir on disk
        val out = sessionDir
        sessionDir = null
        sessionId = null
        photos.clear()
        return out
    }

    @Synchronized
    fun isActive(): Boolean = sessionDir != null

    @Synchronized
    fun currentRelativePath(): String? = sessionId?.let { "SixtScanner/$it/" }

    private fun writePhotosJson() {
        val dir = sessionDir ?: return
        val arr = JSONArray()
        photos.values.sortedBy { it.idx }.forEach { arr.put(TelemetryJson.photoToJson(it)) }
        File(dir, "photos.json").writeText(arr.toString(2))
    }

    companion object {
        private val TS_ISO = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        private val TS_FOLDER = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US)
    }
}
