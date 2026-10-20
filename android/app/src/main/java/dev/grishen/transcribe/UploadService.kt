package dev.grishen.transcribe

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.provider.OpenableColumns
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import java.io.FileNotFoundException
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.atomic.AtomicInteger
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

class UploadService : Service() {
    private val executor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val inFlight = AtomicInteger(0)

    @Volatile
    private var activeCall: Call? = null

    private var lastProgressUptime = 0L
    private var lastPercent = -1

    private val notificationManager by lazy {
        getSystemService(NotificationManager::class.java)
    }

    private val contentIntent: PendingIntent by lazy {
        PendingIntent.getActivity(
            this,
            0,
            Intent(this, LauncherActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
    }

    // 200 MB at about 0.5 Mbit/s takes ~55 minutes. callTimeout is the whole upload.
    // writeTimeout applies to each socket write, not the full body.
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(2, TimeUnit.MINUTES)
        .writeTimeout(2, TimeUnit.MINUTES)
        .callTimeout(60, TimeUnit.MINUTES)
        .retryOnConnectionFailure(false)
        .build()

    override fun onCreate() {
        super.onCreate()
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = getString(R.string.notification_channel_description)
        }
        notificationManager.createNotificationChannel(channel)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        inFlight.incrementAndGet()
        ServiceCompat.startForeground(
            this,
            NOTIFICATION_ID,
            ongoingNotification(indeterminate = true, percent = 0, detail = "Starting"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
        )
        val uri = uriFrom(intent)
        if (uri == null) {
            showResult("Error: no video")
            finishJob(startId)
            return START_NOT_STICKY
        }
        try {
            executor.execute {
                try {
                    uploadWithRetries(uri)
                } catch (exception: Exception) {
                    val detail = exception.message?.trim()?.take(140)
                        ?.ifEmpty { null }
                        ?: exception.javaClass.simpleName
                    showResult("Error: $detail")
                } finally {
                    finishJob(startId)
                }
            }
        } catch (rejected: RejectedExecutionException) {
            showResult("Error: cancelled")
            finishJob(startId)
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        activeCall?.cancel()
        executor.shutdownNow()
        super.onDestroy()
    }

    private fun uploadWithRetries(uri: Uri) {
        val settings = AppSettings.read(this)
        if (settings == null) {
            showResult(getString(R.string.notification_settings_missing))
            return
        }
        val length = contentLength(uri)
        if (length < 0L) {
            showResult("Error: unknown file size")
            return
        }
        var attempt = 0
        while (true) {
            try {
                uploadOnce(uri, length, settings)
                showResult("Uploaded")
                return
            } catch (notFound: FileNotFoundException) {
                showResult("Error: cannot read the video")
                return
            } catch (security: SecurityException) {
                showResult("Error: cannot read the video")
                return
            } catch (http: HttpUploadException) {
                showResult("Error: ${formatHttp(http)}")
                return
            } catch (io: IOException) {
                if (hasCause<FileNotFoundException>(io) || hasCause<SecurityException>(io)) {
                    showResult("Error: cannot read the video")
                    return
                }
                if (!isRetryable(io) || attempt >= MAX_RETRIES) {
                    showResult("Error: ${shortMessage(io)}")
                    return
                }
                attempt += 1
                val delayMs = 2_000L shl (attempt - 1)
                notifyOngoing(
                    indeterminate = true,
                    percent = 0,
                    detail = "Retry $attempt of $MAX_RETRIES",
                )
                try {
                    Thread.sleep(delayMs)
                } catch (interrupted: InterruptedException) {
                    showResult("Error: cancelled")
                    return
                }
            }
        }
    }

    private fun uploadOnce(uri: Uri, length: Long, settings: UploadSettings) {
        lastPercent = -1
        lastProgressUptime = 0L
        val url = "http://${settings.server}/upload"
        val body = StreamingRequestBody(
            openStream = {
                contentResolver.openInputStream(uri)
                    ?: throw FileNotFoundException(uri.toString())
            },
            length = length,
            onProgress = { sent -> publishProgress(sent, length) },
        )
        val request = Request.Builder()
            .url(url)
            .header("Authorization", "Bearer ${settings.token}")
            .put(body)
            .build()
        val call = client.newCall(request)
        activeCall = call
        try {
            call.execute().use { response ->
                if (response.code == 201) {
                    return
                }
                val responseBody = try {
                    response.body.string().take(500)
                } catch (io: IOException) {
                    ""
                }
                throw HttpUploadException(response.code, responseBody)
            }
        } finally {
            if (activeCall === call) {
                activeCall = null
            }
        }
    }

    private fun contentLength(uri: Uri): Long {
        contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                val index = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (index >= 0 && !cursor.isNull(index)) {
                    val size = cursor.getLong(index)
                    if (size >= 0L) {
                        return size
                    }
                }
            }
        }
        contentResolver.openAssetFileDescriptor(uri, "r")?.use { descriptor ->
            if (descriptor.length >= 0L) {
                return descriptor.length
            }
        }
        return -1L
    }

    private fun publishProgress(sent: Long, total: Long) {
        val percent = ((sent * 100) / total).toInt().coerceIn(0, 100)
        val now = SystemClock.elapsedRealtime()
        val done = sent >= total
        if (!done && percent == lastPercent && now - lastProgressUptime < PROGRESS_INTERVAL_MS) {
            return
        }
        lastPercent = percent
        lastProgressUptime = now
        notifyOngoing(indeterminate = false, percent = percent, detail = "$percent%")
    }

    private fun notifyOngoing(indeterminate: Boolean, percent: Int, detail: String) {
        notificationManager.notify(
            NOTIFICATION_ID,
            ongoingNotification(indeterminate, percent, detail),
        )
    }

    private fun ongoingNotification(
        indeterminate: Boolean,
        percent: Int,
        detail: String,
    ) = baseBuilder()
        .setOngoing(true)
        .setContentTitle(getString(R.string.notification_uploading))
        .setContentText(detail)
        .setProgress(100, if (indeterminate) 0 else percent, indeterminate)
        .setCategory(NotificationCompat.CATEGORY_PROGRESS)
        .build()

    private fun showResult(text: String) {
        Log.i(TAG, text)
        val notification = baseBuilder()
            .setOngoing(false)
            .setAutoCancel(true)
            .setContentTitle(text)
            .setProgress(0, 0, false)
            .setCategory(NotificationCompat.CATEGORY_STATUS)
            .build()
        notificationManager.notify(NOTIFICATION_ID, notification)
    }

    private fun baseBuilder(): NotificationCompat.Builder {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setOnlyAlertOnce(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setContentIntent(contentIntent)
    }

    private fun finishJob(startId: Int) {
        val remaining = inFlight.decrementAndGet()
        mainHandler.post {
            if (remaining == 0 && inFlight.get() == 0) {
                stopForeground(STOP_FOREGROUND_DETACH)
            }
            stopSelf(startId)
        }
    }

    private fun isRetryable(error: IOException): Boolean {
        if (activeCall?.isCanceled() == true) {
            return false
        }
        return !hasCause<FileNotFoundException>(error) && !hasCause<SecurityException>(error)
    }

    private fun shortMessage(error: IOException): String {
        val text = error.message?.trim().orEmpty()
        if (text.isEmpty()) {
            return error.javaClass.simpleName
        }
        return text.take(140)
    }

    private fun formatHttp(error: HttpUploadException): String {
        val detail = DETAIL_JSON.find(error.responseBody)?.groupValues?.get(1)?.trim().orEmpty()
        return if (detail.isEmpty()) "HTTP ${error.code}" else "HTTP ${error.code} $detail"
    }

    private inline fun <reified T : Throwable> hasCause(error: Throwable): Boolean {
        var current: Throwable? = error
        while (current != null) {
            if (current is T) {
                return true
            }
            current = current.cause
        }
        return false
    }

    private class HttpUploadException(
        val code: Int,
        val responseBody: String,
    ) : Exception()

    companion object {
        private const val TAG = "UploadService"
        private const val CHANNEL_ID = "uploads"
        private const val NOTIFICATION_ID = 1001
        private const val MAX_RETRIES = 3
        private const val PROGRESS_INTERVAL_MS = 400L
        private val DETAIL_JSON = Regex("\"detail\"\\s*:\\s*\"([^\"]*)\"")

        private fun uriFrom(intent: Intent?): Uri? {
            if (intent == null) {
                return null
            }
            val clip = intent.clipData
            if (clip != null && clip.itemCount > 0) {
                val uri = clip.getItemAt(0).uri
                if (uri != null) {
                    return uri
                }
            }
            return intent.data
        }
    }
}
