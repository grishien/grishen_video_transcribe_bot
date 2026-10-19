package dev.grishen.transcribe

import java.io.InputStream
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody
import okio.BufferedSink

/** Writes the video straight from [openStream] into the socket. The file is never buffered whole. */
class StreamingRequestBody(
    private val openStream: () -> InputStream,
    private val length: Long,
    private val onProgress: (bytesSent: Long) -> Unit,
) : RequestBody() {
    override fun contentType(): MediaType = MIME

    override fun contentLength(): Long = length

    override fun isOneShot(): Boolean = true

    override fun writeTo(sink: BufferedSink) {
        openStream().use { input ->
            val buffer = ByteArray(BUFFER_SIZE)
            var sent = 0L
            while (true) {
                val read = input.read(buffer)
                if (read < 0) {
                    break
                }
                sink.write(buffer, 0, read)
                sent += read
                onProgress(sent)
            }
        }
    }

    private companion object {
        val MIME: MediaType = "video/mp4".toMediaType()
        const val BUFFER_SIZE = 64 * 1024
    }
}
