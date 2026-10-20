package dev.grishen.transcribe

import android.content.Context

data class UploadSettings(val server: String, val token: String)

data class StoredSettings(val server: String, val token: String)

/** App-private server and token. The upload service reads them when an upload starts. */
object AppSettings {
    private const val PREFS = "upload_settings"
    private const val KEY_SERVER = "server"
    private const val KEY_TOKEN = "token"

    private val SERVER = Regex("""^([A-Za-z0-9.-]+):(\d{1,5})$""")

    fun stored(context: Context): StoredSettings {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return StoredSettings(
            server = prefs.getString(KEY_SERVER, "").orEmpty(),
            token = prefs.getString(KEY_TOKEN, "").orEmpty(),
        )
    }

    /** Valid settings, or null when nothing usable is stored. */
    fun read(context: Context): UploadSettings? {
        val stored = stored(context)
        val server = normalizeServer(stored.server) ?: return null
        val token = stored.token.trim()
        if (token.isEmpty()) {
            return null
        }
        return UploadSettings(server, token)
    }

    /**
     * Stores a normalized host:port and a trimmed token.
     * Returns false when the values are invalid or the write fails.
     */
    fun save(context: Context, serverRaw: String, tokenRaw: String): Boolean {
        val server = normalizeServer(serverRaw) ?: return false
        val token = tokenRaw.trim()
        if (token.isEmpty()) {
            return false
        }
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER, server)
            .putString(KEY_TOKEN, token)
            .commit()
    }

    /** `host:port` with a numeric port from 1 to 65535, or null. No scheme. */
    fun normalizeServer(input: String): String? {
        val match = SERVER.matchEntire(input.trim()) ?: return null
        val host = match.groupValues[1]
        val portText = match.groupValues[2]
        if (portText.length > 1 && portText.startsWith('0')) {
            return null
        }
        val port = portText.toIntOrNull() ?: return null
        if (port !in 1..65535) {
            return null
        }
        if (!isIpv4(host) && !isHostname(host)) {
            return null
        }
        return "$host:$port"
    }

    private fun isIpv4(host: String): Boolean {
        val parts = host.split('.')
        if (parts.size != 4) {
            return false
        }
        return parts.all { part ->
            part.isNotEmpty() &&
                part.length <= 3 &&
                !(part.length > 1 && part.startsWith('0')) &&
                part.all { it.isDigit() } &&
                (part.toIntOrNull() ?: -1) in 0..255
        }
    }

    private fun isHostname(host: String): Boolean {
        if (host.length > 253 || host.endsWith('.')) {
            return false
        }
        val labels = host.split('.')
        if (labels.isEmpty()) {
            return false
        }
        return labels.all { label ->
            label.length in 1..63 &&
                label.first().isLetterOrDigit() &&
                label.last().isLetterOrDigit() &&
                label.all { it.isLetterOrDigit() || it == '-' } &&
                label.any { it.isLetter() }
        }
    }
}
