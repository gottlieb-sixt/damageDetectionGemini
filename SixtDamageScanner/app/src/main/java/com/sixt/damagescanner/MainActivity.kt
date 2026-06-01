package com.sixt.damagescanner

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.navigation.NavController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import coil.compose.AsyncImage
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executor

class MainActivity : ComponentActivity() {

    private val vm: ScanViewModel by viewModels()

    private val cameraPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* result handled in UI */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            cameraPermLauncher.launch(Manifest.permission.CAMERA)
        }
        setContent {
            SixtTheme { AppRoot(vm) }
        }
    }
}

@Composable
fun SixtTheme(content: @Composable () -> Unit) {
    val colors = darkColorScheme(
        primary = Color(0xFFFF5F00),
        background = Color.Black,
        surface = Color(0xFF1C1C1E),
        onPrimary = Color.White,
        onBackground = Color.White,
        onSurface = Color.White,
    )
    MaterialTheme(colorScheme = colors, content = content)
}

@Composable
fun AppRoot(vm: ScanViewModel) {
    val nav = rememberNavController()
    NavHost(navController = nav, startDestination = "capture") {
        composable("capture") { CaptureScreen(vm, nav) }
        composable("results") { ResultsScreen(vm, nav) }
        composable("settings"){ SettingsScreen(vm, nav) }
    }
}

// ────────── Inline pill controls (top of capture screen) ──────────
@Composable
private fun ModelPill(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier.clip(RoundedCornerShape(8.dp))
            .background(if (selected) Color(0xFFFF5F00) else Color.White.copy(alpha = 0.06f))
            .border(1.dp, if (selected) Color(0xFFFF5F00) else Color.White.copy(alpha = 0.15f), RoundedCornerShape(8.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(label, color = Color.White, fontSize = 12.sp,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal)
    }
}

@Composable
private fun ResPill(maxSide: Int, onClick: () -> Unit) {
    val label = if (maxSide >= 4000) "📐 Max" else "📐 ${maxSide}"
    val on = maxSide > 1280
    Box(
        Modifier.clip(RoundedCornerShape(8.dp))
            .background(if (on) Color(0xFF3CB4FF).copy(alpha = 0.7f) else Color.White.copy(alpha = 0.06f))
            .border(1.dp, if (on) Color(0xFF3CB4FF) else Color.White.copy(alpha = 0.15f), RoundedCornerShape(8.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp),
    ) {
        Text(label, color = Color.White, fontSize = 11.sp,
            fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal)
    }
}

@Composable
private fun TilePill(on: Boolean, onClick: () -> Unit) {
    Box(
        Modifier.clip(RoundedCornerShape(8.dp))
            .background(if (on) Color(0xFFFF5F00).copy(alpha = 0.85f) else Color.White.copy(alpha = 0.06f))
            .border(1.dp, if (on) Color(0xFFFF5F00) else Color.White.copy(alpha = 0.15f), RoundedCornerShape(8.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp),
    ) {
        Text(if (on) "Tile 3×3 ●" else "Tile 3×3 ○",
            color = Color.White, fontSize = 11.sp,
            fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal)
    }
}

@Composable
private fun ResetPill(enabled: Boolean, onClick: () -> Unit) {
    Box(
        Modifier.clip(RoundedCornerShape(8.dp))
            .background(Color.White.copy(alpha = if (enabled) 0.06f else 0.02f))
            .border(1.dp, Color.White.copy(alpha = if (enabled) 0.15f else 0.05f), RoundedCornerShape(8.dp))
            .clickable(enabled = enabled, onClick = onClick)
            .padding(horizontal = 8.dp, vertical = 6.dp),
    ) {
        Text("↻", color = Color.White.copy(alpha = if (enabled) 0.85f else 0.25f), fontSize = 13.sp)
    }
}

// ────────── SETTINGS ──────────
@Composable
fun SettingsScreen(vm: ScanViewModel, nav: NavController) {
    val ui by vm.ui.collectAsState()
    var key by remember { mutableStateOf(ui.apiKey) }
    Column(
        Modifier.fillMaxSize().background(Color.Black)
            .statusBarsPadding()
            .navigationBarsPadding()
            .padding(20.dp),
    ) {
        Spacer(Modifier.height(8.dp))
        TextButton(onClick = { nav.popBackStack() }) { Text("← Zurück", color = Color.White.copy(alpha = 0.7f)) }
        Spacer(Modifier.height(16.dp))
        Text("Sixt LLM Gateway", color = Color.White, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(6.dp))
        Text("Endpoint: https://llm.orange.sixt.com/v1/chat/completions",
            color = Color.White.copy(alpha = 0.5f), fontSize = 11.sp,
            fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace)
        Spacer(Modifier.height(16.dp))
        Card { Column(Modifier.padding(14.dp)) {
            Text("API-KEY", color = Color.White.copy(alpha = 0.4f), fontSize = 10.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            BasicTextField(
                value = key, onValueChange = { key = it },
                textStyle = androidx.compose.ui.text.TextStyle(color = Color.White, fontSize = 14.sp,
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace),
                modifier = Modifier.fillMaxWidth(),
                cursorBrush = androidx.compose.ui.graphics.SolidColor(Color(0xFFFF5F00)),
            )
            Spacer(Modifier.height(8.dp))
            Text("Format: sk-xxxxxxxxxxxxxxxxxxx  (aus pipeline/annotation_tool/.env)",
                color = Color.White.copy(alpha = 0.4f), fontSize = 10.sp)
        }}
        Spacer(Modifier.height(16.dp))
        Button(onClick = { vm.setApiKey(key); nav.popBackStack() },
            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFFF5F00)),
            modifier = Modifier.fillMaxWidth().height(48.dp), shape = RoundedCornerShape(12.dp)) {
            Text("Speichern", fontSize = 15.sp)
        }
    }
}

// ────────── CAPTURE (live camera + review) ──────────
@Composable
fun CaptureScreen(vm: ScanViewModel, nav: NavController) {
    val ui by vm.ui.collectAsState()
    val currentView = ui.views[ui.currentIdx]
    val currentPhoto = ui.photos[currentView]
    val isLive = currentPhoto == null

    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val executor: Executor = remember { ContextCompat.getMainExecutor(context) }
    val previewView = remember {
        PreviewView(context).apply { scaleType = PreviewView.ScaleType.FILL_CENTER }
    }
    val imageCapture = remember {
        ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
            .build()
    }
    var capturing by remember { mutableStateOf(false) }
    var cameraError by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        try {
            val provider = ProcessCameraProvider.getInstance(context).get()
            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            provider.unbindAll()
            provider.bindToLifecycle(
                lifecycleOwner,
                CameraSelector.DEFAULT_BACK_CAMERA,
                preview,
                imageCapture,
            )
        } catch (e: Exception) {
            cameraError = "Kamera-Init: ${e.message}"
        }
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        // Background: always-on camera preview
        AndroidView(factory = { previewView }, modifier = Modifier.fillMaxSize())
        // Overlay just-captured photo while reviewing
        currentPhoto?.let { p ->
            AsyncImage(
                model = p.file,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        }

        // ── Top overlay: settings + progress + done + pills + dots + view label ──
        Column(
            Modifier.align(Alignment.TopCenter).fillMaxWidth()
                .background(Brush.verticalGradient(
                    listOf(Color.Black.copy(alpha = 0.75f), Color.Black.copy(alpha = 0.0f))
                ))
                .statusBarsPadding()
                .padding(top = 6.dp, bottom = 12.dp),
        ) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = { nav.navigate("settings") }) { Text("⚙️", fontSize = 18.sp) }
                Spacer(Modifier.weight(1f))
                Text("${ui.currentIdx + 1} / ${ui.views.size}",
                    color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                Spacer(Modifier.weight(1f))
                TextButton(
                    onClick = { vm.finalizeSession(); nav.navigate("results") },
                    enabled = ui.photos.isNotEmpty(),
                ) {
                    Text("Fertig →", color = Color(0xFFFF7733))
                }
            }
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                ModelPill("Pro", ui.model == "gemini") { vm.setModel("gemini") }
                Spacer(Modifier.width(6.dp))
                ModelPill("Flash", ui.model == "flash") { vm.setModel("flash") }
                Spacer(Modifier.weight(1f))
                ResPill(ui.maxSide) { vm.cycleMaxSide() }
                Spacer(Modifier.width(6.dp))
                TilePill(ui.tileMode) { vm.setTileMode(!ui.tileMode) }
                Spacer(Modifier.width(6.dp))
                ResetPill(enabled = ui.photos.isNotEmpty()) { vm.resetSession() }
            }
            Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), horizontalArrangement = Arrangement.Center) {
                ui.views.forEachIndexed { i, v ->
                    val photo = ui.photos[v]
                    val status = when {
                        photo?.status == PhotoStatus.DONE -> "done"
                        photo?.status == PhotoStatus.PENDING -> "pending"
                        photo?.status == PhotoStatus.ERROR -> "error"
                        i == ui.currentIdx -> "current"
                        else -> "empty"
                    }
                    val color = when (status) {
                        "done" -> Color(0xFF10B981)
                        "pending" -> Color(0xFFFBBF24)
                        "error" -> Color.Red
                        "current" -> Color(0xFFFF5F00)
                        else -> Color.White.copy(alpha = 0.35f)
                    }
                    Box(Modifier.padding(horizontal = 3.dp)
                        .size(width = if (status == "current") 24.dp else 8.dp, height = 8.dp)
                        .background(color, RoundedCornerShape(4.dp)))
                }
            }
            // View label
            Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(viewIcon(currentView), fontSize = 26.sp)
                Spacer(Modifier.width(8.dp))
                Column {
                    Text(viewLabel(currentView), color = Color.White, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                    Text(viewHint(currentView), color = Color.White.copy(alpha = 0.75f), fontSize = 11.sp)
                }
            }
        }

        cameraError?.let {
            Box(Modifier.align(Alignment.Center).padding(24.dp)) {
                Card { Text(it, color = Color.Red, modifier = Modifier.padding(16.dp)) }
            }
        }

        // ── Bottom overlay: shutter OR retake+next ──
        Column(
            Modifier.align(Alignment.BottomCenter).fillMaxWidth()
                .background(Brush.verticalGradient(
                    listOf(Color.Black.copy(alpha = 0.0f), Color.Black.copy(alpha = 0.85f))
                ))
                .navigationBarsPadding()
                .padding(bottom = 20.dp, top = 32.dp),
        ) {
            ui.error?.let {
                Text(it, color = Color.Red, fontSize = 11.sp,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp)
                        .fillMaxWidth(), textAlign = TextAlign.Center)
            }
            if (isLive) {
                // Shutter
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                    Box(
                        Modifier.size(82.dp).clip(CircleShape)
                            .background(Color.White.copy(alpha = 0.18f)),
                        contentAlignment = Alignment.Center,
                    ) {
                        Button(
                            onClick = {
                                if (capturing) return@Button
                                capturing = true
                                val outFile = createPhotoFile(context, currentView)
                                val opts = ImageCapture.OutputFileOptions.Builder(outFile).build()
                                imageCapture.takePicture(opts, executor, object : ImageCapture.OnImageSavedCallback {
                                    override fun onImageSaved(result: ImageCapture.OutputFileResults) {
                                        capturing = false
                                        vm.submitPhoto(currentView, outFile)
                                    }
                                    override fun onError(exc: ImageCaptureException) {
                                        capturing = false
                                        cameraError = "Aufnahme: ${exc.message}"
                                    }
                                })
                            },
                            modifier = Modifier.size(68.dp),
                            shape = CircleShape,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = if (capturing) Color.Gray else Color.White,
                            ),
                            contentPadding = PaddingValues(0.dp),
                        ) {}
                    }
                }
            } else {
                // Retake + Next
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    OutlinedButton(
                        onClick = { vm.retakeCurrent() },
                        modifier = Modifier.weight(1f).height(54.dp),
                        shape = RoundedCornerShape(14.dp),
                    ) { Text("↻  Wiederholen", color = Color.White, fontSize = 15.sp, fontWeight = FontWeight.Medium) }
                    Button(
                        onClick = {
                            if (ui.currentIdx < ui.views.size - 1) {
                                vm.nextView()
                            } else {
                                vm.finalizeSession()
                                nav.navigate("results")
                            }
                        },
                        modifier = Modifier.weight(1f).height(54.dp),
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFFF5F00)),
                        shape = RoundedCornerShape(14.dp),
                    ) {
                        Text(
                            if (ui.currentIdx < ui.views.size - 1) "Weiter →" else "Auswertung →",
                            fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                        )
                    }
                }
            }
        }
    }
}

private fun createPhotoFile(context: Context, view: String): File {
    val dir = File(context.cacheDir, "captures").apply { mkdirs() }
    val ts = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
    return File(dir, "${view}_${ts}.jpg")
}

// ────────── RESULTS ──────────
@Composable
fun ResultsScreen(vm: ScanViewModel, nav: NavController) {
    val ui by vm.ui.collectAsState()
    val photoList = ui.views.mapNotNull { ui.photos[it] }
    val totalDamages = photoList.sumOf { it.damages.size }
    val pending = photoList.count { it.status == PhotoStatus.PENDING }

    Column(
        Modifier.fillMaxSize().background(Color.Black)
            .statusBarsPadding()
            .navigationBarsPadding(),
    ) {
        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = { vm.resetSession(); nav.popBackStack() }) {
                Text("← Neuer Scan", color = Color.White.copy(alpha = 0.7f))
            }
            Spacer(Modifier.weight(1f))
            Text("${ui.photos.size} Bilder · ${ui.model}",
                color = Color.White, fontSize = 12.sp,
                fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace)
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { nav.popBackStack() }) {
                Text("+ Mehr", color = Color(0xFFFF7733))
            }
        }

        LazyColumn(Modifier.padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item {
                Card {
                    Column(Modifier.padding(14.dp)) {
                        Text("AUSWERTUNG", color = Color.White.copy(alpha = 0.4f),
                            fontSize = 10.sp, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(8.dp))
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceAround) {
                            StatCol("${photoList.size}", "Bilder")
                            StatCol("$totalDamages", "Schäden")
                            StatCol(if (pending > 0) "⏳$pending" else "✓",
                                "Status",
                                color = if (pending > 0) Color(0xFFFBBF24) else Color(0xFF10B981))
                        }
                    }
                }
            }

            items(photoList) { p -> PhotoResultCard(p) }

            ui.sessionPath?.let { path ->
                item {
                    Card(colors = CardDefaults.cardColors(containerColor = Color.White.copy(alpha = 0.04f))) {
                        Column(Modifier.padding(12.dp)) {
                            Text("LOGS GESPEICHERT", color = Color.White.copy(alpha = 0.4f),
                                fontSize = 9.sp, fontWeight = FontWeight.Bold)
                            Spacer(Modifier.height(4.dp))
                            Text("Android/data/com.sixt.damagescanner/$path",
                                color = Color.White.copy(alpha = 0.7f),
                                fontSize = 10.sp,
                                fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace)
                            Spacer(Modifier.height(6.dp))
                            Text(
                                "adb pull /sdcard/Android/data/com.sixt.damagescanner/files/SixtScanner/",
                                color = Color(0xFFFF7733).copy(alpha = 0.85f),
                                fontSize = 9.sp,
                                fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun StatCol(value: String, label: String, color: Color = Color.White) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text(value, color = color, fontSize = 24.sp, fontWeight = FontWeight.Bold)
        Text(label.uppercase(), color = Color.White.copy(alpha = 0.5f), fontSize = 9.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
fun PhotoResultCard(p: LocalPhoto) {
    var imgW by remember { mutableStateOf(0) }
    var imgH by remember { mutableStateOf(0) }

    Card {
        Column {
            Box(Modifier.fillMaxWidth()) {
                AsyncImage(
                    model = p.file,
                    contentDescription = null,
                    contentScale = ContentScale.FillWidth,
                    modifier = Modifier.fillMaxWidth().onSizeChanged { size ->
                        imgW = size.width; imgH = size.height
                    },
                )
                if (imgW > 0 && p.damages.isNotEmpty()) {
                    Canvas(Modifier.matchParentSize()) {
                        p.damages.forEach { d ->
                            val bb = d.bbox_2d
                            if (bb.size != 4) return@forEach
                            val (ymin, xmin, ymax, xmax) = bb
                            val x = (xmin / 1000.0 * size.width).toFloat()
                            val y = (ymin / 1000.0 * size.height).toFloat()
                            val w = ((xmax - xmin) / 1000.0 * size.width).toFloat()
                            val h = ((ymax - ymin) / 1000.0 * size.height).toFloat()
                            val color = colorForLabel(d.label, d._is_cluster)
                            drawRect(color = color,
                                topLeft = Offset(x, y),
                                size = Size(w, h),
                                style = Stroke(width = 4f))
                        }
                    }
                }
                Box(Modifier.padding(8.dp).background(Color.Black.copy(alpha = 0.7f), RoundedCornerShape(8.dp))
                    .padding(horizontal = 8.dp, vertical = 4.dp)) {
                    Text("${viewIcon(p.view)} ${viewLabel(p.view)}", color = Color.White,
                        fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                }
            }
            Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                val text = when (p.status) {
                    PhotoStatus.PENDING -> "⏳ Lädt..."
                    PhotoStatus.DONE -> "${p.damages.size} Schäden"
                    PhotoStatus.ERROR -> "Fehler"
                }
                val color = when (p.status) {
                    PhotoStatus.PENDING -> Color(0xFFFBBF24)
                    PhotoStatus.ERROR -> Color.Red
                    else -> Color.White
                }
                Text(text, color = color, fontSize = 12.sp)
                Spacer(Modifier.weight(1f))
                if (p.status == PhotoStatus.DONE) {
                    Text("%.1fs".format(p.latencyS), color = Color.White.copy(alpha = 0.5f), fontSize = 11.sp)
                }
            }
        }
    }
}

// ────────── Helpers ──────────
fun viewIcon(v: String): String = when {
    v.startsWith("TYRE") -> "🛞"
    v.contains("REAR") -> "🚙"
    else -> "🚗"
}

fun viewLabel(v: String): String = mapOf(
    "FRONT_STRAIGHT" to "Front geradeaus",
    "DIAGONAL_FRONT_LEFT" to "Front-links 45°",
    "DIAGONAL_FRONT_RIGHT" to "Front-rechts 45°",
    "SIDE_LEFT" to "Linke Seite",
    "SIDE_RIGHT" to "Rechte Seite",
    "DIAGONAL_REAR_LEFT" to "Heck-links 45°",
    "DIAGONAL_REAR_RIGHT" to "Heck-rechts 45°",
    "REAR_STRAIGHT" to "Heck geradeaus",
    "TYRE_FRONT_LEFT" to "Reifen vorne links",
    "TYRE_FRONT_RIGHT" to "Reifen vorne rechts",
    "TYRE_REAR_LEFT" to "Reifen hinten links",
    "TYRE_REAR_RIGHT" to "Reifen hinten rechts",
)[v] ?: v

fun viewHint(v: String): String = mapOf(
    "FRONT_STRAIGHT" to "Vor dem Auto, frontal",
    "DIAGONAL_FRONT_LEFT" to "Ecke vorne links, schräg",
    "DIAGONAL_FRONT_RIGHT" to "Ecke vorne rechts, schräg",
    "SIDE_LEFT" to "Längs zur Fahrerseite",
    "SIDE_RIGHT" to "Längs zur Beifahrerseite",
    "DIAGONAL_REAR_LEFT" to "Ecke hinten links",
    "DIAGONAL_REAR_RIGHT" to "Ecke hinten rechts",
    "REAR_STRAIGHT" to "Hinter dem Auto, frontal",
    "TYRE_FRONT_LEFT" to "Nahaufnahme Rad+Felge",
    "TYRE_FRONT_RIGHT" to "Nahaufnahme Rad+Felge",
    "TYRE_REAR_LEFT" to "Nahaufnahme Rad+Felge",
    "TYRE_REAR_RIGHT" to "Nahaufnahme Rad+Felge",
)[v] ?: ""

fun colorForLabel(label: String, isCluster: Boolean): Color = when {
    isCluster -> Color(0xFFFBBF24)
    label == "scratch" -> Color(0xFFFF5050)
    label == "stone_chip" -> Color(0xFFFFA000)
    label == "dent" -> Color(0xFF3CB4FF)
    label == "crack" -> Color(0xFFC83CFF)
    label == "missing" -> Color(0xFFFF3CC8)
    label == "major" -> Color(0xFFFF143C)
    else -> Color(0xFF787878)
}
