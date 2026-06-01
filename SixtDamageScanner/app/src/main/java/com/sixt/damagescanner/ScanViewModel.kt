package com.sixt.damagescanner

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.sixt.damagescanner.llm.DamageAnalyzer
import com.sixt.damagescanner.llm.LlmGatewayClient
import com.sixt.damagescanner.logging.SessionLogger
import kotlinx.coroutines.Job
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
    val damages: List<LlmGatewayClient.Damage> = emptyList(),
    val latencyS: Double = 0.0,
    val error: String? = null,
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
            "FRONT_STRAIGHT", "DIAGONAL_FRONT_LEFT", "DIAGONAL_FRONT_RIGHT",
            "SIDE_LEFT", "SIDE_RIGHT",
            "DIAGONAL_REAR_LEFT", "DIAGONAL_REAR_RIGHT", "REAR_STRAIGHT",
            "TYRE_FRONT_LEFT", "TYRE_FRONT_RIGHT",
            "TYRE_REAR_LEFT", "TYRE_REAR_RIGHT",
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
        if (logger.isActive()) logger.removePhoto(v)
        _ui.value = s.copy(photos = s.photos - v, error = null)
    }

    fun submitPhoto(view: String, file: File) {
        jobs[view]?.cancel()
        captureTimes[view] = System.currentTimeMillis()
        jobs[view] = viewModelScope.launch { runSubmit(view, file) }
    }

    /** Called from UI right before nav.navigate("results") to flush + finalize. */
    fun finalizeSession() {
        if (logger.isActive()) {
            val dir = logger.finalize(reason = "user_finished")
            _ui.value = _ui.value.copy(
                sessionPath = dir?.absolutePath?.substringAfter("/files/")?.let { "files/$it" }
            )
        }
    }

    private suspend fun runSubmit(view: String, file: File) {
        val s0 = _ui.value
        if (s0.apiKey.isBlank()) {
            _ui.value = s0.copy(error = "API-Key fehlt → Settings ⚙️")
            return
        }
        // Lazy-start the session on the first submitted photo so the logger sees
        // the actual chosen plate/model/tile-mode at that moment.
        if (!logger.isActive()) {
            logger.start(model = s0.model, tileMode = s0.tileMode, plate = s0.plate)
        }
        // mark pending
        _ui.value = s0.copy(
            photos = s0.photos + (view to LocalPhoto(view, file, PhotoStatus.PENDING)),
            error = null,
        )
        try {
            val result = DamageAnalyzer.analyze(
                file,
                DamageAnalyzer.Options(
                    apiKey = s0.apiKey,
                    model = s0.model,
                    tileMode = s0.tileMode,
                    view = view,
                    maxSide = s0.maxSide,
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
                    damages = result.damages,
                    latencyS = result.latencyS,
                    error = result.error,
                )),
            )
            // Persist telemetry
            val idx = cur.views.indexOf(view).coerceAtLeast(0)
            val capturedAt = captureTimes[view] ?: System.currentTimeMillis()
            logger.recordPhoto(view, idx, file, result, capturedAt, s0.maxSide)
        } catch (e: Exception) {
            val cur = _ui.value
            if (cur.photos[view]?.file != file) return
            _ui.value = cur.copy(
                photos = cur.photos + (view to LocalPhoto(
                    view = view,
                    file = file,
                    status = PhotoStatus.ERROR,
                    error = e.message,
                )),
                error = e.message,
            )
        }
    }
}
