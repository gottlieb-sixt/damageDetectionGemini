package com.sixt.damagescanner.logging

import android.content.Context
import android.graphics.BitmapFactory
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
 *   session.json   — laufende Session-Metadaten + Aggregate
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
    private var expectedViews: List<String> = emptyList()
    private val photos: MutableMap<String, PhotoTelemetry> = linkedMapOf()  // keyed by view

    @Synchronized
    fun start(model: String, tileMode: Boolean, plate: String, expectedViews: List<String> = emptyList()): String {
        // If a previous session is still open, finalize it best-effort
        if (sessionDir != null) finalize(reason = "auto_close_on_start")

        startedAtMs = System.currentTimeMillis()
        this.modelKey = model
        this.modelId = Pricing.modelIdFor(model)
        this.tileMode = tileMode
        this.plate = plate
        this.expectedViews = expectedViews
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
        writeSessionJson(finalized = false, reason = "in_progress")
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
        photoModel: String = modelKey,
        photoTileMode: Boolean = tileMode,
        autoRetryCount: Int = 0,
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
            model = photoModel,
            modelId = Pricing.modelIdFor(photoModel),
            tileMode = photoTileMode,
            maxSideSetting = maxSideSetting,
            autoRetryCount = autoRetryCount,
            calls = result.calls,
            nPreNms = result.nPreNms,
            nPostNms = result.nPostNms,
            nAfterClusterFilter = result.damages.size,
            nReflectionClusters = result.nReflectionClusters,
            finalDamages = result.damages,
        )
        photos[view] = telemetry
        writePhotosJson()
        writeSessionJson(finalized = false, reason = "in_progress")
    }

    @Synchronized
    fun recordErrorPhoto(
        view: String,
        idx: Int,
        originalCacheFile: File,
        capturedAtMs: Long,
        maxSideSetting: Int,
        error: String,
        photoModel: String = modelKey,
        photoTileMode: Boolean = tileMode,
        autoRetryCount: Int = 0,
    ) {
        val dir = sessionDir ?: return
        val targetName = "%02d_%s.jpg".format(idx + 1, view)
        val targetFile = File(dir, targetName)
        try {
            if (originalCacheFile.exists()) originalCacheFile.copyTo(targetFile, overwrite = true)
        } catch (_: Exception) { /* keep going — error metadata still matters */ }

        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(originalCacheFile.absolutePath, opts)
        val resolution = if (opts.outWidth > 0 && opts.outHeight > 0) {
            opts.outWidth to opts.outHeight
        } else {
            0 to 0
        }

        val telemetry = PhotoTelemetry(
            view = view,
            idx = idx,
            capturedAtIso = TS_ISO.format(Date(capturedAtMs)),
            originalFileName = targetName,
            originalSizeBytes = targetFile.length(),
            originalResolution = resolution,
            sentResolution = resolution,
            model = photoModel,
            modelId = Pricing.modelIdFor(photoModel),
            tileMode = photoTileMode,
            maxSideSetting = maxSideSetting,
            autoRetryCount = autoRetryCount,
            calls = listOf(
                CallTelemetry(
                    tileIdx = null,
                    httpStatus = 0,
                    bytesSent = 0,
                    bytesReceived = 0,
                    latencyMs = 0,
                    promptTokens = 0,
                    completionTokens = 0,
                    error = error,
                    damages = emptyList(),
                )
            ),
            nPreNms = 0,
            nPostNms = 0,
            nAfterClusterFilter = 0,
            nReflectionClusters = 0,
            finalDamages = emptyList(),
        )
        photos[view] = telemetry
        writePhotosJson()
        writeSessionJson(finalized = false, reason = "in_progress")
    }

    @Synchronized
    fun removePhoto(view: String) {
        val dir = sessionDir ?: return
        photos.remove(view)
        // Clean up the file too if present
        dir.listFiles()?.firstOrNull { it.name.endsWith("_$view.jpg") }?.delete()
        writePhotosJson()
        writeSessionJson(finalized = false, reason = "in_progress")
    }

    @Synchronized
    fun finalize(reason: String = "normal"): File? {
        val dir = sessionDir ?: return null
        writeSessionJson(finalized = true, reason = reason)

        // Reset state — keep dir on disk
        val out = sessionDir
        sessionDir = null
        sessionId = null
        expectedViews = emptyList()
        photos.clear()
        return out
    }

    @Synchronized
    fun isActive(): Boolean = sessionDir != null

    @Synchronized
    fun currentRelativePath(): String? = sessionId?.let { "SixtScanner/$it/" }

    private fun writeSessionJson(finalized: Boolean, reason: String) {
        val dir = sessionDir ?: return
        val now = System.currentTimeMillis()
        val photoList = photos.values.toList()
        val completedViews = photoList.sortedBy { it.idx }.map { it.view }
        val missingViews = expectedViews.filterNot { it in completedViews }
        val latencies = photoList.map { it.totalLatencyS }
        val avgLatency = if (latencies.isNotEmpty()) latencies.average() else 0.0
        val p95Latency = latencies.sorted().let {
            if (it.isEmpty()) 0.0 else it[(it.size * 0.95).toInt().coerceAtMost(it.size - 1)]
        }

        val session = JSONObject().apply {
            put("log_schema_version", 2)
            put("session_id", sessionId)
            put("status", if (finalized) "finalized" else "in_progress")
            put("finalized", finalized)
            put("started_at", TS_ISO.format(Date(startedAtMs)))
            put("ended_at", if (finalized) TS_ISO.format(Date(now)) else JSONObject.NULL)
            put("updated_at", TS_ISO.format(Date(now)))
            put("duration_s", (now - startedAtMs) / 1000.0)
            put("plate", plate)
            put("app_version", "0.1.0")
            put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("android_sdk", Build.VERSION.SDK_INT)
            put("close_reason", if (finalized) reason else JSONObject.NULL)
            put("session_settings", JSONObject().apply {
                put("model", modelKey)
                put("model_id", modelId)
                put("tile_mode", tileMode)
            })
            put("expected_views", JSONArray(expectedViews))
            put("completed_views", JSONArray(completedViews))
            put("missing_views", JSONArray(missingViews))
            put("aggregates", JSONObject().apply {
                put("n_expected_views", expectedViews.size)
                put("n_completed_views", completedViews.size)
                put("n_missing_views", missingViews.size)
                put("n_photos", photoList.size)
                put("n_damages_total", photoList.sumOf { it.finalDamages.size })
                put("n_errors", photoList.count { it.hadError })
                put("n_retries", photoList.sumOf { it.retryCount })
                put("n_auto_retries", photoList.sumOf { it.autoRetryCount })
                put("total_bytes_sent", photoList.sumOf { it.totalBytesSent })
                put("total_bytes_received", photoList.sumOf { it.totalBytesReceived })
                put("total_prompt_tokens", photoList.sumOf { it.totalPromptTokens })
                put("total_completion_tokens", photoList.sumOf { it.totalCompletionTokens })
                put("total_usd_estimated", photoList.sumOf { it.totalUsd })
                put("avg_latency_s", avgLatency)
                put("p95_latency_s", p95Latency)
            })
        }
        File(dir, "session.json").writeText(session.toString(2))
    }

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
