package com.sixt.damagescanner.logging

/**
 * Public Google Vertex AI pricing (USD per 1M tokens) as of 2026-01.
 * Sixt LLM-Gateway internal rates may differ — adjust here when the
 * actual contract numbers are confirmed.
 */
object Pricing {

    data class Price(val inUsdPerMTok: Double, val outUsdPerMTok: Double)

    val MODEL_PRICES: Map<String, Price> = mapOf(
        "vertex_ai/gemini-3.1-pro" to Price(inUsdPerMTok = 1.25, outUsdPerMTok = 5.00),
        "vertex_ai/gemini-3.5-flash" to Price(inUsdPerMTok = 0.075, outUsdPerMTok = 0.30),
    )

    fun usd(modelId: String, promptTokens: Int, completionTokens: Int): Double {
        val p = MODEL_PRICES[modelId] ?: return 0.0
        return promptTokens / 1_000_000.0 * p.inUsdPerMTok +
                completionTokens / 1_000_000.0 * p.outUsdPerMTok
    }

    fun modelIdFor(modelKey: String): String = when (modelKey) {
        "flash" -> "vertex_ai/gemini-3.5-flash"
        else -> "vertex_ai/gemini-3.1-pro"
    }
}
