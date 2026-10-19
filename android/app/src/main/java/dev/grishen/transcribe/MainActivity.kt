package dev.grishen.transcribe

import android.app.Activity
import android.content.ClipData
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.core.content.ContextCompat

/**
 * Share target only. Theme.NoDisplay requires [finish] before the first resume,
 * so this activity never shows a window.
 */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        try {
            handleSend(intent)
        } finally {
            finish()
        }
    }

    private fun handleSend(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND) {
            return
        }
        val uri = streamUri(intent)
        if (uri == null) {
            Toast.makeText(this, "Error: no video", Toast.LENGTH_LONG).show()
            return
        }
        val serviceIntent = Intent(this, UploadService::class.java).apply {
            data = uri
            clipData = ClipData.newRawUri("video", uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        try {
            ContextCompat.startForegroundService(this, serviceIntent)
        } catch (exception: RuntimeException) {
            val detail = exception.message?.take(140) ?: "could not start upload"
            Toast.makeText(this, "Error: $detail", Toast.LENGTH_LONG).show()
        }
    }

    private fun streamUri(intent: Intent): Uri? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(Intent.EXTRA_STREAM)
        }
    }
}
