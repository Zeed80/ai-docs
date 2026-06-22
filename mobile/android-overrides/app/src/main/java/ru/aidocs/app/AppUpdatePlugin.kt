package ru.aidocs.app

import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.content.pm.Signature as ApkSignature
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Base64
import androidx.core.content.FileProvider
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import org.json.JSONObject
import java.io.File
import java.io.ByteArrayInputStream
import java.net.URL
import java.security.MessageDigest
import java.security.PublicKey
import java.security.cert.CertificateFactory

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

    private fun fetchManifest(url: String): JSONObject {
        val wrapper = JSONObject(URL(url).readText())
        return verifyManifest(wrapper)
    }

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
                // Android 8+: app needs the per-source "install unknown apps"
                // permission. If missing, send the user to grant it and stop.
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
                    !context.packageManager.canRequestPackageInstalls()
                ) {
                    val settingsIntent = Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${context.packageName}"),
                    ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    context.startActivity(settingsIntent)
                    call.reject("Разрешите установку из этого приложения и повторите")
                    return@Thread
                }

                val apkRel = manifest.optString("url", "/download/latest.apk")
                val apkUrl = resolveApkUrl(apkRel)
                val expectedSha = manifest.optString("sha256", "")

                val outDir = File(context.externalCacheDir, "apk").apply { mkdirs() }
                val apk = File(outDir, "aidocs-latest.apk")
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

                verifyDownloadedApk(apk)

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

    private fun resolveApkUrl(apkRel: String): String {
        val base = baseUrl()
        if (!apkRel.startsWith("http")) return base + apkRel

        val baseParsed = URL(base)
        val apkParsed = URL(apkRel)
        val basePort = if (baseParsed.port == -1) baseParsed.defaultPort else baseParsed.port
        val apkPort = if (apkParsed.port == -1) apkParsed.defaultPort else apkParsed.port
        if (
            baseParsed.protocol != apkParsed.protocol ||
            baseParsed.host != apkParsed.host ||
            basePort != apkPort
        ) {
            throw IllegalArgumentException("Update APK URL must stay on the configured server")
        }
        return apkRel
    }

    private fun verifyDownloadedApk(apk: File) {
        val pm = context.packageManager
        val archive = pm.getPackageArchiveInfo(
            apk.absolutePath,
            PackageManager.GET_SIGNING_CERTIFICATES,
        ) ?: throw IllegalArgumentException("Downloaded APK is not a valid Android package")

        if (archive.packageName != context.packageName) {
            apk.delete()
            throw IllegalArgumentException("Downloaded APK package does not match this app")
        }

        val installed = pm.getPackageInfo(
            context.packageName,
            PackageManager.GET_SIGNING_CERTIFICATES,
        )
        val installedCerts = signingCertFingerprints(installed)
        val archiveCerts = signingCertFingerprints(archive)
        if (installedCerts.isEmpty() || archiveCerts.isEmpty() || installedCerts != archiveCerts) {
            apk.delete()
            throw IllegalArgumentException("Downloaded APK signature does not match this app")
        }
    }

    private fun verifyManifest(wrapper: JSONObject): JSONObject {
        val payloadB64 = wrapper.optString("signedPayload", "")
        val signatureB64 = wrapper.optString("signature", "")
        val alg = wrapper.optString("signatureAlg", "")
        if (payloadB64.isBlank() || signatureB64.isBlank()) {
            throw IllegalArgumentException("Update manifest is not signed")
        }
        if (alg != "SHA256withRSA") {
            throw IllegalArgumentException("Unsupported update manifest signature algorithm")
        }

        val payload = Base64.decode(payloadB64, Base64.DEFAULT)
        val signature = Base64.decode(signatureB64, Base64.DEFAULT)
        val verified = installedSigningPublicKeys().any { publicKey ->
            try {
                val verifier = java.security.Signature.getInstance(alg)
                verifier.initVerify(publicKey)
                verifier.update(payload)
                verifier.verify(signature)
            } catch (_: Exception) {
                false
            }
        }
        if (!verified) {
            throw IllegalArgumentException("Update manifest signature does not match this app")
        }
        return JSONObject(String(payload, Charsets.UTF_8))
    }

    private fun installedSigningPublicKeys(): List<PublicKey> {
        val installed = context.packageManager.getPackageInfo(
            context.packageName,
            PackageManager.GET_SIGNING_CERTIFICATES,
        )
        return signingCertificates(installed).mapNotNull { sig ->
            try {
                val cert = CertificateFactory.getInstance("X.509")
                    .generateCertificate(ByteArrayInputStream(sig.toByteArray()))
                cert.publicKey
            } catch (_: Exception) {
                null
            }
        }.distinctBy { sha256(it.encoded) }
    }

    private fun signingCertFingerprints(info: PackageInfo): Set<String> =
        signingCertificates(info).map { sha256(it.toByteArray()) }.toSet()

    private fun signingCertificates(info: PackageInfo): Array<ApkSignature> {
        val signingInfo = info.signingInfo ?: return emptyArray()
        return if (signingInfo.hasMultipleSigners()) signingInfo.apkContentsSigners
        else signingInfo.signingCertificateHistory
    }

    private fun sha256(bytes: ByteArray): String {
        val md = MessageDigest.getInstance("SHA-256")
        return md.digest(bytes).joinToString("") { "%02x".format(it) }
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
