package com.sixt.damagescanner

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraInfo
import androidx.camera.core.CameraSelector
import android.media.ExifInterface
import com.sixt.damagescanner.llm.LlmGatewayClient
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
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.ui.unit.IntSize
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
        composable("image/{view}") { backStackEntry ->
            ImageZoomScreen(
                vm = vm,
                nav = nav,
                view = backStackEntry.arguments?.getString("view").orEmpty(),
            )
        }
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
@OptIn(ExperimentalCamera2Interop::class)
@Composable
fun CaptureScreen(vm: ScanViewModel, nav: NavController) {
    val ui by vm.ui.collectAsState()
    val currentView = ui.views[ui.currentIdx]
    val currentPhoto = ui.photos[currentView]
    val isLive = currentPhoto == null

    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val executor: Executor = remember { ContextCompat.getMainExecutor(context) }
    val lensOptions = remember(context) { backLensOptions(context) }
    val widestLens = lensOptions.firstOrNull()
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
    var camera by remember { mutableStateOf<Camera?>(null) }
    var zoomRatio by remember { mutableStateOf(1f) }
    var minZoomRatio by remember { mutableStateOf(1f) }
    var maxZoomRatio by remember { mutableStateOf(1f) }
    var useWideLens by remember { mutableStateOf(false) }
    var normalFocalLength by remember { mutableStateOf<Float?>(null) }
    var selectedFocalLength by remember { mutableStateOf<Float?>(null) }
    val hasSeparateWideLens = widestLens != null &&
            (normalFocalLength == null || widestLens.minFocalLength < (normalFocalLength ?: Float.MAX_VALUE) * 0.9f)
    val displayZoomRatio = run {
        val normalFocal = normalFocalLength ?: selectedFocalLength ?: 1f
        val selectedFocal = selectedFocalLength ?: normalFocal
        (selectedFocal / normalFocal * zoomRatio).coerceAtLeast(0.1f)
    }

    LaunchedEffect(useWideLens) {
        try {
            val provider = ProcessCameraProvider.getInstance(context).get()
            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            provider.unbindAll()
            val selector = if (useWideLens && widestLens != null) {
                cameraSelectorForId(widestLens.cameraId)
            } else {
                CameraSelector.DEFAULT_BACK_CAMERA
            }
            camera = provider.bindToLifecycle(
                lifecycleOwner,
                selector,
                preview,
                imageCapture,
            )
            val cameraId = runCatching {
                Camera2CameraInfo.from(camera!!.cameraInfo).cameraId
            }.getOrNull()
            selectedFocalLength = focalLengthForCameraId(context, cameraId)
            if (!useWideLens && selectedFocalLength != null) {
                normalFocalLength = selectedFocalLength
            }
            camera?.cameraInfo?.zoomState?.value?.let { zoomState ->
                zoomRatio = zoomState.zoomRatio
                minZoomRatio = zoomState.minZoomRatio.coerceIn(0.1f, 1f)
                maxZoomRatio = zoomState.maxZoomRatio.coerceAtLeast(1f)
            }
        } catch (e: Exception) {
            if (useWideLens) {
                useWideLens = false
            } else {
                cameraError = "Kamera-Init: ${e.message}"
            }
        }
    }

    Box(Modifier.fillMaxSize().background(Color.Black)) {
        // Background: always-on camera preview
        AndroidView(
            factory = { previewView },
            modifier = Modifier.fillMaxSize()
                .pointerInput(isLive, camera, minZoomRatio, maxZoomRatio) {
                    if (!isLive) return@pointerInput
                    detectTransformGestures { _, _, zoom, _ ->
                        val min = minZoomRatio.coerceIn(0.1f, 1f)
                        val max = maxZoomRatio.coerceAtLeast(1f)
                        val nextZoom = (zoomRatio * zoom).coerceIn(min, max)
                        zoomRatio = nextZoom
                        camera?.cameraControl?.setZoomRatio(nextZoom)
                    }
                },
        )
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
                    onClick = { nav.navigate("results") },
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
                Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                    if (camera != null) {
                        Box(
                            Modifier.clip(RoundedCornerShape(12.dp))
                                .background(Color.Black.copy(alpha = 0.55f))
                                .clickable(enabled = hasSeparateWideLens || minZoomRatio < 0.99f) {
                                    if (hasSeparateWideLens) {
                                        useWideLens = !useWideLens
                                    } else if (minZoomRatio < 0.99f) {
                                        val nextZoom = if (zoomRatio <= minZoomRatio + 0.05f) 1f else minZoomRatio
                                        zoomRatio = nextZoom
                                        camera?.cameraControl?.setZoomRatio(nextZoom)
                                    }
                                }
                                .padding(horizontal = 10.dp, vertical = 4.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(
                                "%.1fx".format(displayZoomRatio),
                                color = Color.White,
                                fontSize = 10.sp,
                                fontWeight = FontWeight.SemiBold,
                                textAlign = TextAlign.Center,
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                    }
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

private data class BackLensOption(
    val cameraId: String,
    val minFocalLength: Float,
)

private fun backLensOptions(context: Context): List<BackLensOption> {
    val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    return manager.cameraIdList.mapNotNull { id ->
        val characteristics = manager.getCameraCharacteristics(id)
        val isBack = characteristics.get(CameraCharacteristics.LENS_FACING) ==
                CameraCharacteristics.LENS_FACING_BACK
        val minFocal = characteristics.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
            ?.minOrNull()
        if (isBack && minFocal != null && minFocal > 0f) BackLensOption(id, minFocal) else null
    }.sortedBy { it.minFocalLength }
}

private fun focalLengthForCameraId(context: Context, cameraId: String?): Float? {
    if (cameraId == null) return null
    val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    return runCatching {
        manager.getCameraCharacteristics(cameraId)
            .get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
            ?.minOrNull()
    }.getOrNull()
}

@OptIn(ExperimentalCamera2Interop::class)
private fun cameraSelectorForId(cameraId: String): CameraSelector =
    CameraSelector.Builder()
        .addCameraFilter { cameraInfos: List<CameraInfo> ->
            cameraInfos.filter { Camera2CameraInfo.from(it).cameraId == cameraId }
        }
        .build()

// ────────── RESULTS ──────────
@Composable
fun ResultsScreen(vm: ScanViewModel, nav: NavController) {
    val ui by vm.ui.collectAsState()
    val photoList = ui.views.mapNotNull { ui.photos[it] }
    val totalDamages = photoList.sumOf { it.damages.size }
    val pending = photoList.count { it.status == PhotoStatus.PENDING }
    val errors = photoList.count { it.status == PhotoStatus.ERROR }

    LaunchedEffect(pending, errors, ui.sessionPath, ui.photos.size) {
        if (ui.photos.isNotEmpty() && pending == 0 && errors == 0 && ui.sessionPath == null) {
            vm.finalizeSession()
        }
    }

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
                            StatCol(if (pending > 0) "⏳$pending" else if (errors > 0) "!$errors" else "✓",
                                "Status",
                                color = when {
                                    pending > 0 -> Color(0xFFFBBF24)
                                    errors > 0 -> Color.Red
                                    else -> Color(0xFF10B981)
                                })
                        }
                        if (pending > 0) {
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "$pending Bilder werden noch verarbeitet. Logs werden gespeichert, sobald alles fertig ist.",
                                color = Color(0xFFFBBF24),
                                fontSize = 11.sp,
                                textAlign = TextAlign.Center,
                                modifier = Modifier.fillMaxWidth(),
                            )
                        } else if (errors > 0) {
                            Spacer(Modifier.height(8.dp))
                            Text(
                                "$errors Fehlerbilder werden automatisch erneut versucht. Du kannst zusätzlich manuell Retry drücken.",
                                color = Color.Red.copy(alpha = 0.9f),
                                fontSize = 11.sp,
                                textAlign = TextAlign.Center,
                                modifier = Modifier.fillMaxWidth(),
                            )
                        }
                    }
                }
            }

            items(photoList) { p ->
                PhotoResultCard(
                    p = p,
                    onImageClick = { nav.navigate("image/${p.view}") },
                    onRetry = { vm.retryPhoto(p.view) },
                )
            }

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
fun PhotoResultCard(p: LocalPhoto, onImageClick: () -> Unit, onRetry: () -> Unit) {
    Card {
        Column {
            Box(Modifier.fillMaxWidth().clickable(onClick = onImageClick)) {
                AnnotatedPhoto(
                    photo = p,
                    modifier = Modifier.fillMaxWidth().height(220.dp),
                    contentScale = ContentScale.Fit,
                    showBadges = true,
                )
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
                Text("Tippen zum Zoomen", color = Color.White.copy(alpha = 0.45f), fontSize = 10.sp)
                if (p.status == PhotoStatus.DONE) {
                    Spacer(Modifier.width(8.dp))
                    Text("%.1fs".format(p.latencyS), color = Color.White.copy(alpha = 0.5f), fontSize = 11.sp)
                }
                if (p.status == PhotoStatus.ERROR) {
                    Spacer(Modifier.width(8.dp))
                    if (p.autoRetrying) {
                        Text("Auto-Retry #${p.autoRetryCount + 1}", color = Color.Red.copy(alpha = 0.75f), fontSize = 10.sp)
                        Spacer(Modifier.width(8.dp))
                    }
                    TextButton(onClick = onRetry, contentPadding = PaddingValues(horizontal = 8.dp, vertical = 0.dp)) {
                        Text("Retry", color = Color(0xFFFF7733), fontSize = 12.sp)
                    }
                }
            }
            if (p.status == PhotoStatus.DONE) {
                if (p.damages.isEmpty()) {
                    Text(
                        "✓ Keine Schäden erkannt",
                        color = Color(0xFF4ADE80),
                        fontSize = 12.sp,
                        modifier = Modifier.padding(start = 12.dp, end = 12.dp, bottom = 12.dp),
                    )
                } else {
                    Column(Modifier.padding(start = 12.dp, end = 12.dp, bottom = 12.dp)) {
                        p.damages.forEachIndexed { i, d ->
                            if (i > 0) HorizontalDivider(color = Color.White.copy(alpha = 0.08f))
                            DamageRow(d)
                        }
                    }
                }
            }
            p.error?.let { err ->
                Text(
                    err,
                    color = Color.Red.copy(alpha = 0.9f),
                    fontSize = 10.sp,
                    modifier = Modifier.padding(start = 12.dp, end = 12.dp, bottom = 12.dp),
                )
            }
        }
    }
}

@Composable
fun ImageZoomScreen(vm: ScanViewModel, nav: NavController, view: String) {
    val ui by vm.ui.collectAsState()
    val photo = ui.photos[view]
    var scale by remember { mutableStateOf(1f) }
    var offset by remember { mutableStateOf(Offset.Zero) }
    var viewport by remember { mutableStateOf(IntSize.Zero) }

    if (photo == null) {
        LaunchedEffect(Unit) { nav.popBackStack() }
        return
    }

    Box(
        Modifier.fillMaxSize()
            .background(Color.Black)
            .statusBarsPadding()
            .navigationBarsPadding()
    ) {
        Box(
            Modifier.fillMaxSize()
                .onGloballyPositioned { viewport = it.size }
                .pointerInput(Unit) {
                    detectTransformGestures { _, pan, zoom, _ ->
                        val nextScale = (scale * zoom).coerceIn(1f, 6f)
                        val maxX = ((viewport.width * (nextScale - 1f)) / 2f).coerceAtLeast(0f)
                        val maxY = ((viewport.height * (nextScale - 1f)) / 2f).coerceAtLeast(0f)
                        scale = nextScale
                        offset = Offset(
                            (offset.x + pan.x).coerceIn(-maxX, maxX),
                            (offset.y + pan.y).coerceIn(-maxY, maxY),
                        )
                    }
                },
            contentAlignment = Alignment.Center,
        ) {
            AnnotatedPhoto(
                photo = photo,
                modifier = Modifier.fillMaxSize()
                    .graphicsLayer {
                        scaleX = scale
                        scaleY = scale
                        translationX = offset.x
                        translationY = offset.y
                    },
                contentScale = ContentScale.Fit,
                showBadges = true,
            )
        }

        Row(
            Modifier.align(Alignment.TopCenter)
                .fillMaxWidth()
                .background(Color.Black.copy(alpha = 0.72f))
                .padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = { nav.popBackStack() }) {
                Text("← Zurück", color = Color.White)
            }
            Spacer(Modifier.weight(1f))
            Text(
                "Pinch zum Zoomen",
                color = Color.White.copy(alpha = 0.65f),
                fontSize = 11.sp,
            )
            Spacer(Modifier.width(10.dp))
            TextButton(onClick = {
                scale = 1f
                offset = Offset.Zero
            }) {
                Text("Reset", color = Color(0xFFFF7733))
            }
        }
    }
}

@Composable
fun AnnotatedPhoto(
    photo: LocalPhoto,
    modifier: Modifier = Modifier,
    contentScale: ContentScale = ContentScale.FillWidth,
    showBadges: Boolean = true,
) {
    var viewSize by remember { mutableStateOf(IntSize.Zero) }
    // EXIF-corrected size: Coil rotates the displayed image per the orientation
    // flag, so the overlay must use the same upright dimensions (w/h swapped for
    // 90°/270°). Otherwise the bbox geometry is computed against the wrong frame.
    val imageSize = remember(photo.file) {
        val raw = BitmapFactory.Options().run {
            inJustDecodeBounds = true
            BitmapFactory.decodeFile(photo.file.absolutePath, this)
            IntSize(outWidth.coerceAtLeast(0), outHeight.coerceAtLeast(0))
        }
        val orientation = try {
            ExifInterface(photo.file.absolutePath)
                .getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        } catch (e: Exception) {
            ExifInterface.ORIENTATION_NORMAL
        }
        val swap = orientation == ExifInterface.ORIENTATION_ROTATE_90 ||
            orientation == ExifInterface.ORIENTATION_ROTATE_270 ||
            orientation == ExifInterface.ORIENTATION_TRANSPOSE ||
            orientation == ExifInterface.ORIENTATION_TRANSVERSE
        if (swap) IntSize(raw.height, raw.width) else raw
    }

    Box(modifier) {
        AsyncImage(
            model = photo.file,
            contentDescription = null,
            contentScale = contentScale,
            modifier = Modifier.matchParentSize().onSizeChanged { size ->
                viewSize = size
            },
        )
        if (viewSize.width > 0 && imageSize.width > 0 && imageSize.height > 0 && photo.damages.isNotEmpty()) {
            Canvas(Modifier.matchParentSize()) {
                val imageRect = imageContentRect(
                    containerWidth = size.width,
                    containerHeight = size.height,
                    imageWidth = imageSize.width.toFloat(),
                    imageHeight = imageSize.height.toFloat(),
                    contentScale = contentScale,
                )
                photo.damages.forEach { d ->
                    val bb = d.bbox_2d
                    if (bb.size != 4) return@forEach
                    val (ymin, xmin, ymax, xmax) = bb
                    val x = imageRect.left + (xmin / 1000.0 * imageRect.width).toFloat()
                    val y = imageRect.top + (ymin / 1000.0 * imageRect.height).toFloat()
                    val w = ((xmax - xmin) / 1000.0 * imageRect.width).toFloat()
                    val h = ((ymax - ymin) / 1000.0 * imageRect.height).toFloat()
                    val color = colorForLabel(d.label, d._is_cluster)
                    drawRect(color = color,
                        topLeft = Offset(x, y),
                        size = Size(w, h),
                        style = Stroke(width = 4f))
                }
            }
        }
        if (showBadges) {
                Box(Modifier.padding(8.dp).background(Color.Black.copy(alpha = 0.7f), RoundedCornerShape(8.dp))
                    .padding(horizontal = 8.dp, vertical = 4.dp)) {
                    Text("${viewIcon(photo.view)} ${viewLabel(photo.view)}", color = Color.White,
                        fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
                }
                Box(
                    Modifier.align(Alignment.TopEnd)
                        .padding(8.dp)
                        .background(Color.Black.copy(alpha = 0.7f), RoundedCornerShape(8.dp))
                        .padding(horizontal = 8.dp, vertical = 4.dp)
                ) {
                    Text(
                        analysisBadge(photo),
                        color = Color.White,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.SemiBold,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                    )
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

fun analysisBadge(p: LocalPhoto): String {
    val model = when (p.model) {
        "gemini" -> "Pro"
        "flash" -> "Flash"
        "" -> "Model?"
        else -> p.model
    }
    val res = if (p.maxSide > 0) p.maxSide.toString() else "Res?"
    val tile = if (p.tileMode) "Tile" else "Single"
    return "$model · $res · $tile"
}

private data class ImageDrawRect(
    val left: Float,
    val top: Float,
    val width: Float,
    val height: Float,
)

private fun imageContentRect(
    containerWidth: Float,
    containerHeight: Float,
    imageWidth: Float,
    imageHeight: Float,
    contentScale: ContentScale,
): ImageDrawRect {
    val scale = when (contentScale) {
        ContentScale.Crop -> maxOf(containerWidth / imageWidth, containerHeight / imageHeight)
        ContentScale.FillWidth -> containerWidth / imageWidth
        ContentScale.FillHeight -> containerHeight / imageHeight
        ContentScale.None -> 1f
        else -> minOf(containerWidth / imageWidth, containerHeight / imageHeight)
    }
    val drawnWidth = imageWidth * scale
    val drawnHeight = imageHeight * scale
    return ImageDrawRect(
        left = (containerWidth - drawnWidth) / 2f,
        top = (containerHeight - drawnHeight) / 2f,
        width = drawnWidth,
        height = drawnHeight,
    )
}

@Composable
fun DamageRow(d: LlmGatewayClient.Damage) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 6.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Box(
            Modifier.padding(top = 3.dp)
                .size(11.dp)
                .clip(CircleShape)
                .background(colorForLabel(d.label, d._is_cluster)),
        )
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                val title = if (d._is_cluster)
                    "${labelDe(d.label)} · ${d._cluster_size}× Cluster"
                else labelDe(d.label)
                Text(title, color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.weight(1f))
                if (d.confidence > 0.0) {
                    Text(
                        "${(d.confidence * 100).toInt()}%",
                        color = Color.White.copy(alpha = 0.5f),
                        fontSize = 11.sp,
                    )
                }
            }
            val where = d.panel?.takeIf { it.isNotBlank() }?.let { panelDe(it) }
                ?: positionHint(d.bbox_2d)
            val sev = d.severity?.takeIf { it.isNotBlank() }?.let { " · ${severityDe(it)}" } ?: ""
            Text(
                "📍 $where$sev",
                color = Color.White.copy(alpha = 0.7f),
                fontSize = 11.sp,
            )
            val reason = d.reasoning
            if (!reason.isNullOrBlank()) {
                Text(
                    reason,
                    color = Color.White.copy(alpha = 0.5f),
                    fontSize = 11.sp,
                    lineHeight = 14.sp,
                    modifier = Modifier.padding(top = 1.dp),
                )
            }
        }
    }
}

fun labelDe(label: String): String = when (label) {
    "scratch" -> "Kratzer"
    "stone_chip" -> "Steinschlag"
    "dent" -> "Delle"
    "crack" -> "Riss"
    "missing" -> "Fehlendes Teil"
    "major" -> "Schwerer Schaden"
    "other" -> "Sonstiges"
    else -> label.replace('_', ' ').replaceFirstChar { it.uppercase() }
}

fun severityDe(severity: String): String = when (severity.lowercase()) {
    "light" -> "leicht"
    "medium" -> "mittel"
    "severe" -> "schwer"
    else -> severity
}

/** Prettify the model's snake_case English panel name (e.g. "driver_door" → "Driver door"). */
fun panelDe(panel: String): String =
    panel.replace('_', ' ').trim().replaceFirstChar { it.uppercase() }

/** Fallback location when the model gives no panel: derive a rough position from the bbox center. */
fun positionHint(bbox: List<Double>): String {
    if (bbox.size != 4) return "unbekannte Position"
    val cy = (bbox[0] + bbox[2]) / 2.0   // ymin, ymax
    val cx = (bbox[1] + bbox[3]) / 2.0   // xmin, xmax
    val vert = when {
        cy < 333 -> "oben"
        cy > 666 -> "unten"
        else -> "mittig"
    }
    val horiz = when {
        cx < 333 -> "links"
        cx > 666 -> "rechts"
        else -> "Mitte"
    }
    return "$vert $horiz"
}

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
