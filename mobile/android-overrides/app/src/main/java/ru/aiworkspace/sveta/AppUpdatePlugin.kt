package ru.aiworkspace.sveta

import android.content.Intent
import androidx.core.content.FileProvider
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.security.MessageDigest

/**
 * Self-update from the site (no Play Store): reads /download/version.json, compares
 * versionCode, downloads the signed APK, verifies sha256, and launches the system
 * installer. Update is applied "over the top" only because the same release keystore
 * signs every build (see mobile/README.md).
 */
@CapacitorPlugin(name = "AppUpdate")
class AppUpdatePlugin : Plugin() {

    private fun baseUrl(): String {
        // Resolve relative /download/* against the user-configured server (origin).
        // Falls back to the origin of the currently loaded page. No host is hardcoded.
        ServerConfigPlugin.savedUrl(context)?.let { return it.trimEnd('/') }
        val current = bridge.webView?.url ?: return ""
        return try {
            val u = URL(current)
            val port = if (u.port == -1) "" else ":${u.port}"
            "${u.protocol}://${u.host}$port"
        } catch (_: Exception) {
            ""
        }
    }

    private fun currentVersionCode(): Long {
        val pm = context.packageManager
        val info = pm.getPackageInfo(context.packageName, 0)
        return if (android.os.Build.VERSION.SDK_INT >= 28) info.longVersionCode
        else @Suppress("DEPRECATION") info.versionCode.toLong()
    }

    private fun fetchManifest(url: String): JSONObject =
        JSONObject(URL(url).readText())

    @PluginMethod
    fun checkForUpdate(call: PluginCall) {
        val rel = call.getString("url") ?: "/download/version.json"
        Thread {
            try {
                val manifest = fetchManifest(baseUrl() + rel)
                val latest = manifest.optLong("versionCode", -1)
                val available = latest > currentVersionCode()
                val res = JSObject()
                res.put("available", available)
                res.put("versionName", manifest.optString("versionName"))
                res.put("versionCode", latest)
                res.put("changelog", manifest.optString("changelog"))
                call.resolve(res)
            } catch (e: Exception) {
                val res = JSObject()
                res.put("available", false)
                res.put("error", e.message)
                call.resolve(res)
            }
        }.start()
    }

    @PluginMethod
    fun downloadAndInstall(call: PluginCall) {
        val rel = call.getString("url") ?: "/download/version.json"
        Thread {
            try {
                val manifest = fetchManifest(baseUrl() + rel)
                val apkRel = manifest.optString("url", "/download/latest.apk")
                val apkUrl = if (apkRel.startsWith("http")) apkRel else baseUrl() + apkRel
                val expectedSha = manifest.optString("sha256", "")

                val outDir = File(context.externalCacheDir, "apk").apply { mkdirs() }
                val apk = File(outDir, "sveta-latest.apk")
                URL(apkUrl).openStream().use { input ->
                    apk.outputStream().use { input.copyTo(it) }
                }

                if (expectedSha.isNotEmpty()) {
                    val actual = sha256(apk)
                    if (!actual.equals(expectedSha, ignoreCase = true)) {
                        apk.delete()
                        call.reject("Checksum mismatch")
                        return@Thread
                    }
                }

                val uri = FileProvider.getUriForFile(
                    context, "${context.packageName}.fileprovider", apk,
                )
                val install = Intent(Intent.ACTION_VIEW).apply {
                    setDataAndType(uri, "application/vnd.android.package-archive")
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                context.startActivity(install)
                call.resolve()
            } catch (e: Exception) {
                call.reject(e.message ?: "Update failed")
            }
        }.start()
    }

    private fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { ins ->
            val buf = ByteArray(8192)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}
