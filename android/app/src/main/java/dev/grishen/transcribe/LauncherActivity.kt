package dev.grishen.transcribe

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.textfield.TextInputEditText

class LauncherActivity : AppCompatActivity() {
    private val requestNotifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_launcher)
        val serverInput = findViewById<TextInputEditText>(R.id.server_input)
        val tokenInput = findViewById<TextInputEditText>(R.id.token_input)
        val statusMessage = findViewById<TextView>(R.id.status_message)
        if (savedInstanceState == null) {
            val stored = AppSettings.stored(this)
            serverInput.setText(stored.server)
            tokenInput.setText(stored.token)
        }
        findViewById<Button>(R.id.save_button).setOnClickListener {
            val serverText = serverInput.text?.toString().orEmpty()
            val tokenText = tokenInput.text?.toString().orEmpty()
            val error = validationMessage(serverText, tokenText)
            if (error != null) {
                statusMessage.text = error
                return@setOnClickListener
            }
            if (!AppSettings.save(this, serverText, tokenText)) {
                statusMessage.text = getString(R.string.error_save_failed)
                return@setOnClickListener
            }
            val stored = AppSettings.stored(this)
            serverInput.setText(stored.server)
            tokenInput.setText(stored.token)
            statusMessage.text = getString(R.string.saved)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun validationMessage(serverText: String, tokenText: String): String? {
        if (serverText.isBlank() && tokenText.isBlank()) {
            return getString(R.string.error_server_and_token)
        }
        if (serverText.isBlank()) {
            return getString(R.string.error_server_required)
        }
        if (AppSettings.normalizeServer(serverText) == null) {
            return getString(R.string.error_server_invalid)
        }
        if (tokenText.isBlank()) {
            return getString(R.string.error_token_required)
        }
        return null
    }
}
