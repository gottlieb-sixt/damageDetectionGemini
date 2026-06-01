package com.sixt.damagescanner.logging

import com.sixt.damagescanner.llm.LlmGatewayClient
import org.json.JSONArray
import org.json.JSONObject

/** One HTTP round-trip to the LLM gateway (single photo = 1 call, tile-mode = 9). */
data class CallTelemetry(
    val tileIdx: Int?,           // null for single-mode, 0..8 for tile-mode
    val httpStatus: Int,
    val bytesSent: Long,
    val bytesReceived: Long,
    val latencyMs: Long,
    val promptTokens: Int,
    val completionTokens: Int,
    val error: String?,
    val damages: List<LlmGatewayClient.Damage>,
)

/** Everything about one captured photo (one view). */
data class PhotoTelemetry(
    val view: String,
    val idx: Int,
    val capturedAtIso: String,
    val originalFileName: String,
    val originalSizeBytes: Long,
    val originalResolution: Pair<Int, Int>,
    val sentResolution: Pair<Int, Int>,
    val model: String,           // "flash" | "gemini"
    val modelId: String,         // "vertex_ai/gemini-3.5-flash" etc.
    val tileMode: Boolean,
    val maxSideSetting: Int,     // chosen pill value (1280 / 2048 / 4000)
    val calls: List<CallTelemetry>,
    val nPreNms: Int,
    val nPostNms: Int,
    val nAfterClusterFilter: Int,
    val nReflectionClusters: Int,
    val finalDamages: List<LlmGatewayClient.Damage>,
) {
    val totalLatencyS: Double get() = (calls.maxOfOrNull { it.latencyMs } ?: 0L) / 1000.0
    val totalBytesSent: Long get() = calls.sumOf { it.bytesSent }
    val totalBytesReceived: Long get() = calls.sumOf { it.bytesReceived }
    val totalPromptTokens: Int get() = calls.sumOf { it.promptTokens }
    val totalCompletionTokens: Int get() = calls.sumOf { it.completionTokens }
    val totalUsd: Double get() = Pricing.usd(modelId, totalPromptTokens, totalCompletionTokens)
    val hadError: Boolean get() = calls.any { it.error != null }
}

/** JSON serialization helpers — uses org.json to stay consistent with existing code. */
object TelemetryJson {

    fun callToJson(c: CallTelemetry): JSONObject = JSONObject().apply {
        put("tile_idx", c.tileIdx ?: JSONObject.NULL)
        put("http_status", c.httpStatus)
        put("bytes_sent", c.bytesSent)
        put("bytes_received", c.bytesReceived)
        put("latency_ms", c.latencyMs)
        put("prompt_tokens", c.promptTokens)
        put("completion_tokens", c.completionTokens)
        put("error", c.error ?: JSONObject.NULL)
    }

    fun damageToJson(d: LlmGatewayClient.Damage): JSONObject = JSONObject().apply {
        put("bbox_2d", JSONArray(d.bbox_2d))
        put("label", d.label)
        put("confidence", d.confidence)
        put("severity", d.severity ?: JSONObject.NULL)
        put("panel", d.panel ?: JSONObject.NULL)
        put("reasoning", d.reasoning ?: JSONObject.NULL)
        put("is_cluster", d._is_cluster)
        put("cluster_size", d._cluster_size)
        put("source", d._source ?: JSONObject.NULL)
    }

    fun photoToJson(p: PhotoTelemetry): JSONObject = JSONObject().apply {
        put("view", p.view)
        put("idx", p.idx)
        put("captured_at", p.capturedAtIso)
        put("original_file", p.originalFileName)
        put("original_size_bytes", p.originalSizeBytes)
        put("original_resolution", JSONArray(listOf(p.originalResolution.first, p.originalResolution.second)))
        put("sent_resolution", JSONArray(listOf(p.sentResolution.first, p.sentResolution.second)))
        put("model", p.model)
        put("model_id", p.modelId)
        put("tile_mode", p.tileMode)
        put("max_side_setting", p.maxSideSetting)
        put("n_calls", p.calls.size)
        put("calls", JSONArray().apply { p.calls.forEach { put(callToJson(it)) } })
        put("totals", JSONObject().apply {
            put("latency_s", p.totalLatencyS)
            put("bytes_sent", p.totalBytesSent)
            put("bytes_received", p.totalBytesReceived)
            put("prompt_tokens", p.totalPromptTokens)
            put("completion_tokens", p.totalCompletionTokens)
            put("usd_estimated", p.totalUsd)
        })
        put("analysis", JSONObject().apply {
            put("n_pre_nms", p.nPreNms)
            put("n_post_nms", p.nPostNms)
            put("n_after_cluster_filter", p.nAfterClusterFilter)
            put("n_reflection_clusters", p.nReflectionClusters)
            put("damages", JSONArray().apply { p.finalDamages.forEach { put(damageToJson(it)) } })
        })
    }
}
