import java.net.URI
import java.util.Properties
import org.gradle.api.DefaultTask
import org.gradle.api.file.DirectoryProperty
import org.gradle.api.provider.Property
import org.gradle.api.tasks.Input
import org.gradle.api.tasks.OutputDirectory
import org.gradle.api.tasks.TaskAction

plugins {
    id("com.android.application")
}

val localProperties = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) {
        file.inputStream().use { load(it) }
    }
}

fun requiredProperty(name: String): String {
    val value = localProperties.getProperty(name)?.trim().orEmpty()
    if (value.isEmpty()) {
        throw GradleException(
            "$name is missing from android/local.properties. " +
                "Set SERVER_URL=http://<VPS_IP>:<PORT> and UPLOAD_TOKEN to the VPS token " +
                "(at least 32 characters). See local.properties.example.",
        )
    }
    return value
}

fun javaStringLiteral(value: String): String = buildString {
    append('"')
    value.forEach { ch ->
        when (ch) {
            '\\' -> append("\\\\")
            '"' -> append("\\\"")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            else -> append(ch)
        }
    }
    append('"')
}

val serverUrl = requiredProperty("SERVER_URL")
val uploadToken = requiredProperty("UPLOAD_TOKEN")
if (uploadToken.length < 32) {
    throw GradleException(
        "UPLOAD_TOKEN in android/local.properties must be at least 32 characters.",
    )
}

val serverUri = try {
    URI(serverUrl)
} catch (exception: IllegalArgumentException) {
    throw GradleException("SERVER_URL in android/local.properties is not a valid URL.")
}
if (serverUri.scheme != "http" && serverUri.scheme != "https") {
    throw GradleException("SERVER_URL must start with http:// or https://.")
}
val vpsHost = serverUri.host
    ?.trim()
    ?.removePrefix("[")
    ?.removeSuffix("]")
    ?.trim()
    .orEmpty()
if (vpsHost.isEmpty()) {
    throw GradleException("SERVER_URL must include a host.")
}

abstract class GenerateNetworkSecurityConfigTask : DefaultTask() {
    @get:Input
    abstract val cleartextHost: Property<String>

    @get:OutputDirectory
    abstract val outputDirectory: DirectoryProperty

    @TaskAction
    fun generate() {
        val escaped = cleartextHost.get()
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&apos;")
        val xmlDir = outputDirectory.get().asFile.resolve("xml")
        xmlDir.mkdirs()
        xmlDir.resolve("network_security_config.xml").writeText(
            """
            <?xml version="1.0" encoding="utf-8"?>
            <network-security-config>
                <base-config cleartextTrafficPermitted="false" />
                <domain-config cleartextTrafficPermitted="true">
                    <domain includeSubdomains="false">$escaped</domain>
                </domain-config>
            </network-security-config>
            """.trimIndent() + "\n",
            Charsets.UTF_8,
        )
    }
}

val generateNetworkSecurityConfig = tasks.register<GenerateNetworkSecurityConfigTask>(
    "generateNetworkSecurityConfig",
) {
    cleartextHost.set(vpsHost)
}

android {
    namespace = "dev.grishen.transcribe"
    compileSdk {
        version = release(37) {
            minorApiLevel = 2
        }
    }

    defaultConfig {
        applicationId = "dev.grishen.transcribe"
        minSdk = 26
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"
        buildConfigField("String", "SERVER_URL", javaStringLiteral(serverUrl))
        buildConfigField("String", "UPLOAD_TOKEN", javaStringLiteral(uploadToken))
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }
}

androidComponents {
    onVariants { variant ->
        variant.sources.res?.addGeneratedSourceDirectory(
            generateNetworkSecurityConfig,
            GenerateNetworkSecurityConfigTask::outputDirectory,
        )
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.8.0")
    implementation("androidx.activity:activity-ktx:1.13.0")
    implementation("androidx.core:core-ktx:1.19.1")
    implementation("com.google.android.material:material:1.14.0")
    implementation("com.squareup.okhttp3:okhttp:5.5.0")
}
