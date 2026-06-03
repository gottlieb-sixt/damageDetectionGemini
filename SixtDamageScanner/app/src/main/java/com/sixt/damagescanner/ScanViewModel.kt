package com.sixt.damagescanner

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.sixt.damagescanner.llm.DamageAnalyzer
import com.sixt.damagescanner.llm.LlmGatewayClient
import com.sixt.damagescanner.logging.SessionLogger
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File

enum class PhotoStatus { PENDING, DONE, ERROR }

data class LocalPhoto(
    val view: String,
    val file: File,
    val status: PhotoStatus,
    val model: String = "",
    val tileMode: Boolean = false,
    val maxSide: Int = 0,
    val damages: List<LlmGatewayClient.Damage> = emptyList(),
    val latencyS: Double = 0.0,
    val error: String? = null,
    val autoRetryCount: Int = 0,
    val autoRetrying: Boolean = false,
)

data class UiState(
    val apiKey: String = "",
    val plate: String = "",
    val model: String = "flash",
    val tileMode: Boolean = false,
    val maxSide: Int = 1280,
    val views: List<String> = DEFAULT_VIEWS,
    val currentIdx: Int = 0,
    val photos: Map<String, LocalPhoto> = emptyMap(),
    val error: String? = null,
    val sessionPath: String? = null,
) {
    companion object {
        val DEFAULT_VIEWS: List<String> = listOf(
            "FRONT_STRAIGHT",
            "DIAGONAL_FRONT_LEFT",
            "TYRE_FRONT_LEFT",
            "SIDE_LEFT",
            "TYRE_REAR_LEFT",
            "DIAGONAL_REAR_LEFT",
            "REAR_STRAIGHT",
            "DIAGONAL_REAR_RIGHT",
            "TYRE_REAR_RIGHT",
            "SIDE_RIGHT",
            "TYRE_FRONT_RIGHT",
            "DIAGONAL_FRONT_RIGHT",
        )
        val MAX_SIDE_OPTIONS: List<Int> = listOf(1280, 2048, 4000)
    }
}

private const val PREFS = "scanner_prefs"
private const val KEY_API = "api_key"

class ScanViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val _ui = MutableStateFlow(UiState(
        apiKey = prefs.getString(KEY_API, "").orEmpty()
    ))
    val ui: StateFlow<UiState> = _ui.asStateFlow()

    private val logger = SessionLogger(app.applicationContext)
    private val jobs = mutableMapOf<String, Job>()
    private val captureTimes = mutableMapOf<String, Long>()
    private val autoRetryCounts = mutableMapOf<String, Int>()

    fun setApiKey(k: String) {
        prefs.edit().putString(KEY_API, k.trim()).apply()
        _ui.value = _ui.value.copy(apiKey = k.trim())
    }
    fun setPlate(p: String) { _ui.value = _ui.value.copy(plate = p) }
    fun setModel(m: String) { _ui.value = _ui.value.copy(model = m) }
    fun setTileMode(b: Boolean) { _ui.value = _ui.value.copy(tileMode = b) }
    fun cycleMaxSide() {
        val opts = UiState.MAX_SIDE_OPTIONS
        val cur = _ui.value.maxSide
        val next = opts[(opts.indexOf(cur).coerceAtLeast(0) + 1) % opts.size]
        _ui.value = _ui.value.copy(maxSide = next)
    }

    fun startSession() {
        _ui.value = _ui.value.copy(
            currentIdx = 0,
            photos = emptyMap(),
            error = null,
            sessionPath = null,
        )
    }

    fun nextView() {
        val s = _ui.value
        if (s.currentIdx < s.views.size - 1) _ui.value = s.copy(currentIdx = s.currentIdx + 1)
    }

    fun resetSession() {
        jobs.values.forEach { it.cancel() }
        jobs.clear()
        captureTimes.clear()
        autoRetryCounts.clear()
        if (logger.isActive()) logger.finalize(reason = "reset")
        _ui.value = _ui.value.copy(
            currentIdx = 0,
            photos = emptyMap(),
            error = null,
            sessionPath = null,
        )
    }

    fun retakeCurrent() {
        val s = _ui.value
        val v = s.views[s.currentIdx]
        jobs[v]?.cancel()
        jobs.remove(v)
        captureTimes.remove(v)
        autoRetryCounts.remove(v)
        if (logger.isActive()) logger.removePhoto(v)
        _ui.value = s.copy(photos = s.photos - v, error = null)
    }

    fun submitPhoto(view: String, file: File) {
        jobs[view]?.cancel()
        captureTimes[view] = System.currentTimeMillis()
        jobs[view] = viewModelScope.launch { runSubmit(view, file) }
    }

    fun retryPhoto(view: String) {
        val photo = _ui.value.photos[view] ?: return
        if (photo.status == PhotoStatus.PENDING) return
        jobs[view]?.cancel()
        jobs[view] = viewModelScope.launch {
            _ui.value = _ui.value.copy(error = null)
            runSubmit(view, photo.file, retryFrom = photo.copy(autoRetrying = false))
        }
    }

    /** Finalizes only after all submitted photos finished processing. */
    fun finalizeSession(): Boolean {
        val pending = _ui.value.photos.values.count { it.status == PhotoStatus.PENDING }
        if (pending > 0) {
            _ui.value = _ui.value.copy(error = "$pending Bilder werden noch verarbeitet")
            return false
        }
        val errors = _ui.value.photos.values.count { it.status == PhotoStatus.ERROR }
        if (errors > 0) {
            _ui.value = _ui.value.copy(error = "$errors Fehlerbilder werden automatisch erneut versucht")
            return false
        }
        if (logger.isActive()) {
            val dir = logger.finalize(reason = "user_finished")
            _ui.value = _ui.value.copy(
                sessionPath = dir?.absolutePath?.substringAfter("/files/")?.let { "files/$it" }
            )
        }
        return true
    }

    private suspend fun runSubmit(view: String, file: File, retryFrom: LocalPhoto? = null) {
        val s0 = _ui.value
        if (s0.apiKey.isBlank()) {
            _ui.value = s0.copy(error = "API-Key fehlt → Settings ⚙️")
            return
        }
        // Lazy-start the session on the first submitted photo so the logger sees
        // the actual chosen plate/model/tile-mode at that moment.
        if (!logger.isActive()) {
            logger.start(model = s0.model, tileMode = s0.tileMode, plate = s0.plate, expectedViews = s0.views)
        }
        // mark pending
        val model = retryFrom?.model?.takeIf { it.isNotBlank() } ?: s0.model
        val tileMode = retryFrom?.tileMode ?: s0.tileMode
        val maxSide = retryFrom?.maxSide?.takeIf { it > 0 } ?: s0.maxSide
        val autoRetryCount = retryFrom?.autoRetryCount ?: autoRetryCounts[view] ?: 0
        _ui.value = s0.copy(
            photos = s0.photos + (view to LocalPhoto(
                view = view,
                file = file,
                status = PhotoStatus.PENDING,
                model = model,
                tileMode = tileMode,
                maxSide = maxSide,
                autoRetryCount = autoRetryCount,
                autoRetrying = retryFrom?.autoRetrying ?: false,
            )),
            error = null,
        )
        try {
            val result = DamageAnalyzer.analyze(
                file,
                DamageAnalyzer.Options(
                    apiKey = s0.apiKey,
                    model = model,
                    tileMode = tileMode,
                    view = view,
                    maxSide = maxSide,
                ),
            )
            val cur = _ui.value
            // Stale-guard: only apply if user didn't retake meanwhile
            if (cur.photos[view]?.file != file) return
            _ui.value = cur.copy(
                photos = cur.photos + (view to LocalPhoto(
                    view = view,
                    file = file,
                    status = if (result.error != null) PhotoStatus.ERROR else PhotoStatus.DONE,
                    model = model,
                    tileMode = tileMode,
                    maxSide = maxSide,
                    damages = result.damages,
                    latencyS = result.latencyS,
                    error = result.error,
                    autoRetryCount = autoRetryCount,
                    autoRetrying = result.error != null && shouldAutoRetry(result),
                )),
            )
            // Persist telemetry
            val idx = cur.views.indexOf(view).coerceAtLeast(0)
            val capturedAt = captureTimes[view] ?: System.currentTimeMillis()
            logger.recordPhoto(
                view = view,
                idx = idx,
                originalCacheFile = file,
                result = result,
                capturedAtMs = capturedAt,
                maxSideSetting = maxSide,
                photoModel = model,
                photoTileMode = tileMode,
                autoRetryCount = autoRetryCount,
            )
            if (result.error == null) {
                autoRetryCounts.remove(view)
            } else if (shouldAutoRetry(result)) {
                scheduleAutoRetry(view, file, model, tileMode, maxSide)
            }
        } catch (e: Exception) {
            val cur = _ui.value
            if (cur.photos[view]?.file != file) return
            val idx = cur.views.indexOf(view).coerceAtLeast(0)
            val capturedAt = captureTimes[view] ?: System.currentTimeMillis()
            logger.recordErrorPhoto(
                view = view,
                idx = idx,
                originalCacheFile = file,
                capturedAtMs = capturedAt,
                maxSideSetting = maxSide,
                error = e.message ?: e.javaClass.simpleName,
                photoModel = model,
                photoTileMode = tileMode,
                autoRetryCount = autoRetryCount,
            )
            val error = e.message ?: e.javaClass.simpleName
            _ui.value = cur.copy(
                photos = cur.photos + (view to LocalPhoto(
                    view = view,
                    file = file,
                    status = PhotoStatus.ERROR,
                    model = model,
                    tileMode = tileMode,
                    maxSide = maxSide,
                    error = error,
                    autoRetryCount = autoRetryCount,
                    autoRetrying = shouldAutoRetry(error),
                )),
                error = error,
            )
            if (shouldAutoRetry(error)) {
                scheduleAutoRetry(view, file, model, tileMode, maxSide)
            }
        }
    }

    private fun scheduleAutoRetry(view: String, file: File, model: String, tileMode: Boolean, maxSide: Int) {
        val nextRetryCount = (autoRetryCounts[view] ?: 0) + 1
        autoRetryCounts[view] = nextRetryCount
        val delayMs = autoRetryDelayMs(view, nextRetryCount)
        jobs[view] = viewModelScope.launch {
            delay(delayMs)
            val curPhoto = _ui.value.photos[view] ?: return@launch
            if (curPhoto.file != file || curPhoto.status != PhotoStatus.ERROR) return@launch
            runSubmit(
                view = view,
                file = file,
                retryFrom = curPhoto.copy(
                    model = model,
                    tileMode = tileMode,
                    maxSide = maxSide,
                    autoRetryCount = nextRetryCount,
                    autoRetrying = true,
                ),
            )
        }
    }

    private fun autoRetryDelayMs(view: String, retryCount: Int): Long {
        val base = when (retryCount) {
            1 -> 5_000L
            2 -> 10_000L
            else -> 20_000L
        }
        val stagger = _ui.value.views.indexOf(view).coerceAtLeast(0) * 500L
        return base + stagger
    }

    private fun shouldAutoRetry(result: LlmGatewayClient.Result): Boolean =
        result.calls.any { it.httpStatus == 0 || it.httpStatus == 429 || it.httpStatus >= 500 }

    private fun shouldAutoRetry(error: String?): Boolean {
        val e = error?.lowercase().orEmpty()
        return e.contains("unable to resolve host") ||
                e.contains("no address associated with hostname") ||
                e.contains("timeout") ||
                e.contains("timed out") ||
                e.contains("failed to connect") ||
                e.contains("connection reset") ||
                e.contains("network is unreachable") ||
                e.contains("socket")
    }
}
