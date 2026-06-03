package com.sixt.damagescanner.llm

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import android.util.Base64
import com.sixt.damagescanner.logging.CallTelemetry
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

object DamageAnalyzer {

    data class Options(
        val apiKey: String,
        val model: String,        // "gemini" | "flash"
        val tileMode: Boolean,
        val view: String,
        val maxSide: Int = 1280,  // 1280 | 2048 | 4000 (effectively "no resize")
    )

    suspend fun analyze(file: File, opts: Options): LlmGatewayClient.Result {
        val modelId = when (opts.model) {
            "flash" -> "vertex_ai/gemini-3.5-flash"
            else -> "vertex_ai/gemini-3.1-pro"
        }
        val t0 = System.currentTimeMillis()

        val raw = withContext(Dispatchers.IO) {
            val decoded = BitmapFactory.decodeFile(file.absolutePath) ?: error("decode failed")
            applyExifOrientation(file, decoded)
        }
        val originalResolution = raw.width to raw.height
        val full = withContext(Dispatchers.IO) { resize(raw, opts.maxSide) }
        val sentResolution = full.width to full.height

        val calls: List<CallTelemetry>
        val damagesPostNms: List<LlmGatewayClient.Damage>
        val finalDamages: List<LlmGatewayClient.Damage>
        val nPreNms: Int

        if (opts.tileMode) {
            val tiles = makeTiles(full, grid = 3, overlap = 0.10)
            val basePrompt = DamagePrompt.buildCot(opts.view) + DamagePrompt.tileSuffix(opts.view)

            calls = coroutineScope {
                tiles.map { tile ->
                    async(Dispatchers.IO) {
                        val uri = encodeJpegDataUri(tile.bitmap, opts.maxSide)
                        val call = LlmGatewayClient.analyzeCall(opts.apiKey, modelId, uri, basePrompt, tileIdx = tile.idx)
                        // Rebase BBoxes from tile-local (0..1000) to image-global (0..1000)
                        val rebased = call.damages.map { d ->
                            val (ymin, xmin, ymax, xmax) = d.bbox_2d
                            val pxY1 = ymin / 1000.0 * tile.th + tile.yOff
                            val pxY2 = ymax / 1000.0 * tile.th + tile.yOff
                            val pxX1 = xmin / 1000.0 * tile.tw + tile.xOff
                            val pxX2 = xmax / 1000.0 * tile.tw + tile.xOff
                            d.copy(
                                bbox_2d = listOf(
                                    pxY1 / full.height * 1000.0,
                                    pxX1 / full.width * 1000.0,
                                    pxY2 / full.height * 1000.0,
                                    pxX2 / full.width * 1000.0,
                                ),
                                _source = "tile_${tile.idx}",
                            )
                        }
                        call.copy(damages = rebased)
                    }
                }.awaitAll()
            }
            val pre = calls.flatMap { it.damages }
            nPreNms = pre.size
            damagesPostNms = nms(pre, iouThr = 0.4)
            finalDamages = collapseReflectionClusters(damagesPostNms, maxPerCluster = 5, clusterRadius = 120.0)
        } else {
            val uri = encodeJpegDataUri(full, opts.maxSide)
            val prompt = DamagePrompt.buildCot(opts.view)
            val call = LlmGatewayClient.analyzeCall(opts.apiKey, modelId, uri, prompt, tileIdx = null)
                .let { c -> c.copy(damages = c.damages.map { it.copy(_source = "single") }) }
            calls = listOf(call)
            nPreNms = call.damages.size
            damagesPostNms = call.damages  // NMS over single call is identity
            finalDamages = collapseReflectionClusters(call.damages, maxPerCluster = 5, clusterRadius = 120.0)
        }

        val latency = (System.currentTimeMillis() - t0) / 1000.0
        val firstError = calls.firstNotNullOfOrNull { it.error }
        return LlmGatewayClient.Result(
            damages = finalDamages,
            calls = calls,
            nCalls = calls.size,
            nPreNms = nPreNms,
            nPostNms = damagesPostNms.size,
            nReflectionClusters = finalDamages.count { it._is_cluster },
            latencyS = latency,
            originalResolution = originalResolution,
            sentResolution = sentResolution,
            error = firstError,
        )
    }

    // ───────── Bitmap helpers ─────────

    /**
     * Rotates/flips [bm] according to the JPEG's EXIF orientation tag so the
     * bitmap pixels are upright. CameraX writes a 4000×3000 sensor frame plus an
     * orientation flag (typically 6 = rotate 90° CW) rather than rotating pixels.
     * Coil applies this flag when displaying, so we MUST apply it here too — both
     * for the model input and for the overlay coordinate frame to line up.
     */
    private fun applyExifOrientation(file: File, bm: Bitmap): Bitmap {
        val orientation = try {
            ExifInterface(file.absolutePath)
                .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        } catch (e: Exception) {
            ExifInterface.ORIENTATION_NORMAL
        }
        val m = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_ROTATE_90 -> m.postRotate(90f)
            ExifInterface.ORIENTATION_ROTATE_180 -> m.postRotate(180f)
            ExifInterface.ORIENTATION_ROTATE_270 -> m.postRotate(270f)
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> m.postScale(-1f, 1f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> m.postScale(1f, -1f)
            ExifInterface.ORIENTATION_TRANSPOSE -> { m.postRotate(90f); m.postScale(-1f, 1f) }
            ExifInterface.ORIENTATION_TRANSVERSE -> { m.postRotate(270f); m.postScale(-1f, 1f) }
            else -> return bm
        }
        return try {
            Bitmap.createBitmap(bm, 0, 0, bm.width, bm.height, m, true).also {
                if (it != bm) bm.recycle()
            }
        } catch (e: OutOfMemoryError) {
            bm
        }
    }

    private fun resize(bm: Bitmap, maxSide: Int): Bitmap {
        val mx = max(bm.width, bm.height)
        if (mx <= maxSide) return bm
        val r = maxSide.toDouble() / mx
        return Bitmap.createScaledBitmap(bm, (bm.width * r).toInt(), (bm.height * r).toInt(), true)
    }

    private fun encodeJpegDataUri(bm: Bitmap, maxSide: Int, quality: Int = 88): String {
        val src = if (max(bm.width, bm.height) > maxSide) resize(bm, maxSide) else bm
        val baos = ByteArrayOutputStream()
        src.compress(Bitmap.CompressFormat.JPEG, quality, baos)
        val b64 = Base64.encodeToString(baos.toByteArray(), Base64.NO_WRAP)
        return "data:image/jpeg;base64,$b64"
    }

    // ───────── Tile splitter ─────────

    private data class Tile(val bitmap: Bitmap, val xOff: Int, val yOff: Int, val tw: Int, val th: Int, val idx: Int)

    private fun makeTiles(img: Bitmap, grid: Int, overlap: Double): List<Tile> {
        val W = img.width
        val H = img.height
        val stepPct = 1.0 / grid
        val tw = (W * (stepPct + overlap)).toInt()
        val th = (H * (stepPct + overlap)).toInt()
        val tiles = mutableListOf<Tile>()
        var idx = 0
        for (row in 0 until grid) {
            for (col in 0 until grid) {
                var x = (W * stepPct * col).toInt() - (if (col > 0) (W * overlap / 2).toInt() else 0)
                var y = (H * stepPct * row).toInt() - (if (row > 0) (H * overlap / 2).toInt() else 0)
                x = max(0, x)
                y = max(0, y)
                val x2 = min(W, x + tw)
                val y2 = min(H, y + th)
                val w = x2 - x
                val h = y2 - y
                val sub = Bitmap.createBitmap(img, x, y, w, h)
                tiles.add(Tile(sub, x, y, w, h, idx++))
            }
        }
        return tiles
    }

    // ───────── NMS in 0-1000 coords ─────────

    private fun iou1000(a: List<Double>, b: List<Double>): Double {
        val (ya1, xa1, ya2, xa2) = a
        val (yb1, xb1, yb2, xb2) = b
        val ix1 = max(xa1, xb1); val iy1 = max(ya1, yb1)
        val ix2 = min(xa2, xb2); val iy2 = min(ya2, yb2)
        if (ix2 <= ix1 || iy2 <= iy1) return 0.0
        val inter = (ix2 - ix1) * (iy2 - iy1)
        val areaA = max(0.0, xa2 - xa1) * max(0.0, ya2 - ya1)
        val areaB = max(0.0, xb2 - xb1) * max(0.0, yb2 - yb1)
        val union = areaA + areaB - inter
        return if (union > 0) inter / union else 0.0
    }

    private fun nms(damages: List<LlmGatewayClient.Damage>, iouThr: Double): List<LlmGatewayClient.Damage> {
        val sorted = damages.sortedByDescending { it.confidence }
        val kept = mutableListOf<LlmGatewayClient.Damage>()
        for (d in sorted) {
            if (kept.none { iou1000(d.bbox_2d, it.bbox_2d) > iouThr }) {
                kept.add(d)
            }
        }
        return kept
    }

    // ───────── Reflection-cluster collapse ─────────

    private fun collapseReflectionClusters(
        damages: List<LlmGatewayClient.Damage>,
        maxPerCluster: Int,
        clusterRadius: Double,
    ): List<LlmGatewayClient.Damage> {
        if (damages.size <= maxPerCluster) return damages

        val byClass = damages.groupBy { it.label }
        val result = mutableListOf<LlmGatewayClient.Damage>()

        for ((label, dets) in byClass) {
            if (dets.size <= maxPerCluster) {
                result.addAll(dets)
                continue
            }
            val withCenters = dets.map { d ->
                val (ymin, xmin, ymax, xmax) = d.bbox_2d
                Triple(d, (xmin + xmax) / 2.0, (ymin + ymax) / 2.0)
            }
            var unassigned = withCenters.indices.toMutableList()
            val clusters = mutableListOf<List<Int>>()
            while (unassigned.isNotEmpty()) {
                val seed = unassigned[0]
                val (_, sCx, sCy) = withCenters[seed]
                val cluster = mutableListOf(seed)
                val remaining = mutableListOf<Int>()
                for (i in 1 until unassigned.size) {
                    val idx = unassigned[i]
                    val (_, cx, cy) = withCenters[idx]
                    val dist = sqrt((cx - sCx) * (cx - sCx) + (cy - sCy) * (cy - sCy))
                    if (dist < clusterRadius) cluster.add(idx) else remaining.add(idx)
                }
                unassigned = remaining
                clusters.add(cluster)
            }
            for (cluster in clusters) {
                if (cluster.size <= maxPerCluster) {
                    cluster.forEach { result.add(withCenters[it].first) }
                } else {
                    val bboxes = cluster.map { withCenters[it].first.bbox_2d }
                    val mega = listOf(
                        bboxes.minOf { it[0] },
                        bboxes.minOf { it[1] },
                        bboxes.maxOf { it[2] },
                        bboxes.maxOf { it[3] },
                    )
                    val avgConf = cluster.map { withCenters[it].first.confidence }.average()
                    result.add(
                        LlmGatewayClient.Damage(
                            bbox_2d = mega,
                            label = label,
                            confidence = min(0.4, avgConf * 0.5),
                            severity = "uncertain",
                            reasoning = "Cluster von ${cluster.size} $label-Detections — wahrscheinlich Reflexion.",
                            _is_cluster = true,
                            _cluster_size = cluster.size,
                        )
                    )
                }
            }
        }
        return result
    }
}
