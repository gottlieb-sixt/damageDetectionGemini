package com.sixt.damagescanner.llm

import com.sixt.damagescanner.logging.CallTelemetry
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

object LlmGatewayClient {

    private const val GATEWAY_URL = "https://llm.orange.sixt.com/v1/chat/completions"
    private val JSON = "application/json; charset=utf-8".toMediaType()

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    data class Damage(
        val bbox_2d: List<Double>,
        val label: String,
        val confidence: Double,
        val severity: String? = null,
        val panel: String? = null,
        val reasoning: String? = null,
        var _is_cluster: Boolean = false,
        var _cluster_size: Int = 0,
        var _source: String? = null,
    )

    data class Result(
        val damages: List<Damage>,
        val calls: List<CallTelemetry>,
        val nCalls: Int,
        val nPreNms: Int,
        val nPostNms: Int,
        val nReflectionClusters: Int,
        val latencyS: Double,
        val originalResolution: Pair<Int, Int>,
        val sentResolution: Pair<Int, Int>,
        val error: String? = null,
    )

    /**
     * Performs one HTTP call to the LLM Gateway and returns a [CallTelemetry] —
     * never throws for HTTP / parse errors (those land in [CallTelemetry.error]
     * with [CallTelemetry.damages] = empty), but DOES throw for missing key.
     */
    suspend fun analyzeCall(
        apiKey: String,
        modelId: String,
        dataUriJpegBase64: String,
        prompt: String,
        tileIdx: Int? = null,
    ): CallTelemetry = withContext(Dispatchers.IO) {
        if (apiKey.isBlank()) throw IllegalStateException("API-Key fehlt (Settings)")

        val content = JSONArray().apply {
            put(JSONObject().apply {
                put("type", "image_url")
                put("image_url", JSONObject().put("url", dataUriJpegBase64))
            })
            put(JSONObject().apply {
                put("type", "text")
                put("text", prompt)
            })
        }
        val messages = JSONArray().put(JSONObject().apply {
            put("role", "user")
            put("content", content)
        })
        val bodyObj = JSONObject().apply {
            put("model", modelId)
            put("messages", messages)
            put("max_tokens", 8192)
            put("temperature", 0.1)
            put("response_format", JSONObject().put("type", "json_object"))
        }
        val bodyBytes = bodyObj.toString().toByteArray(Charsets.UTF_8)
        val requestBody = bodyBytes.toRequestBody(JSON)

        val req = Request.Builder()
            .url(GATEWAY_URL)
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Content-Type", "application/json")
            .post(requestBody)
            .build()

        val t0 = System.currentTimeMillis()
        try {
            http.newCall(req).execute().use { resp ->
                val respBytes = resp.body?.bytes() ?: ByteArray(0)
                val latencyMs = System.currentTimeMillis() - t0
                val text = String(respBytes, Charsets.UTF_8)

                if (!resp.isSuccessful) {
                    return@withContext CallTelemetry(
                        tileIdx = tileIdx,
                        httpStatus = resp.code,
                        bytesSent = bodyBytes.size.toLong(),
                        bytesReceived = respBytes.size.toLong(),
                        latencyMs = latencyMs,
                        promptTokens = 0,
                        completionTokens = 0,
                        error = "HTTP ${resp.code}: ${text.take(300)}",
                        damages = emptyList(),
                    )
                }

                val root = JSONObject(text)
                val rawContent = root.optJSONArray("choices")
                    ?.optJSONObject(0)
                    ?.optJSONObject("message")
                    ?.optString("content")
                    .orEmpty()
                val damages = parseDamagesJson(rawContent)
                val usage = root.optJSONObject("usage")
                val pTok = usage?.optInt("prompt_tokens", 0) ?: 0
                val cTok = usage?.optInt("completion_tokens", 0) ?: 0

                CallTelemetry(
                    tileIdx = tileIdx,
                    httpStatus = resp.code,
                    bytesSent = bodyBytes.size.toLong(),
                    bytesReceived = respBytes.size.toLong(),
                    latencyMs = latencyMs,
                    promptTokens = pTok,
                    completionTokens = cTok,
                    error = null,
                    damages = damages,
                )
            }
        } catch (e: Exception) {
            CallTelemetry(
                tileIdx = tileIdx,
                httpStatus = 0,
                bytesSent = bodyBytes.size.toLong(),
                bytesReceived = 0L,
                latencyMs = System.currentTimeMillis() - t0,
                promptTokens = 0,
                completionTokens = 0,
                error = e.message ?: e.javaClass.simpleName,
                damages = emptyList(),
            )
        }
    }

    private fun parseDamagesJson(raw: String): List<Damage> {
        val cleaned = raw
            .replace(Regex("^```(?:json)?\\s*", RegexOption.MULTILINE), "")
            .replace(Regex("\\s*```\\s*$", RegexOption.MULTILINE), "")
            .trim()
        if (cleaned.isEmpty()) return emptyList()
        val obj = try {
            JSONObject(cleaned)
        } catch (e: Exception) {
            val m = Regex("\\{[\\s\\S]*\\}").find(cleaned) ?: return emptyList()
            JSONObject(m.value)
        }
        val arr = obj.optJSONArray("damages") ?: obj.optJSONArray("visible_damages") ?: return emptyList()
        val out = mutableListOf<Damage>()
        for (i in 0 until arr.length()) {
            val d = arr.optJSONObject(i) ?: continue
            val bb = d.optJSONArray("bbox_2d") ?: continue
            if (bb.length() != 4) continue
            out.add(
                Damage(
                    bbox_2d = listOf(bb.getDouble(0), bb.getDouble(1), bb.getDouble(2), bb.getDouble(3)),
                    label = d.optString("label", "other"),
                    confidence = d.optDouble("confidence", 0.0),
                    severity = d.optString("severity").ifEmpty { null },
                    panel = d.optString("panel").ifEmpty { null },
                    reasoning = d.optString("reasoning").ifEmpty { null },
                )
            )
        }
        return out
    }
}
