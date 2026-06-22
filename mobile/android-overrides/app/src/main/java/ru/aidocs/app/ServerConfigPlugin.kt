package ru.aidocs.app

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.webkit.CookieManager
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin

/**
 * Stores the server URL the user chose at first launch (typed or scanned from a
 * QR). No server is hardcoded in the app — the bundled launcher reads this and
 * navigates the WebView to the live site. AppUpdate/push also resolve relative
 * paths against this base.
 *
 * Also holds a pending deep-link path from a push tap on cold start.
 */
@CapacitorPlugin(name = "ServerConfig")
class ServerConfigPlugin : Plugin() {

    private fun prefs() = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @PluginMethod
    fun get(call: PluginCall) {
        val res = JSObject()
        res.put("url", prefs().getString(KEY_URL, null))
        call.resolve(res)
    }

    @PluginMethod
    fun set(call: PluginCall) {
        val raw = call.getString("url")?.trim()
        if (raw.isNullOrEmpty()) {
            call.reject("Missing url")
            return
        }
        val url = try {
            normalize(raw)
        } catch (e: IllegalArgumentException) {
            call.reject(e.message ?: "Invalid url")
            return
        }
        val previous = prefs().getString(KEY_URL, null)
        if (previous != null && previous != url) {
            clearMobileState(context)
        }
        prefs().edit().putString(KEY_URL, url).apply()
        val res = JSObject()
        res.put("url", url)
        call.resolve(res)
    }

    @PluginMethod
    fun clear(call: PluginCall) {
        clearMobileState(context)
        // Reload the activity → Capacitor loads the bundled launcher → setup screen.
        activity?.runOnUiThread { activity?.recreate() }
        call.resolve()
    }

    @PluginMethod
    fun consumePendingPath(call: PluginCall) {
        val p = prefs().getString(KEY_PENDING_PATH, null)
        if (p != null) prefs().edit().remove(KEY_PENDING_PATH).apply()
        val res = JSObject()
        res.put("path", p)
        call.resolve(res)
    }

    override fun shouldOverrideLoad(url: Uri): Boolean? {
        val saved = savedUrl(context) ?: return null
        val scheme = url.scheme?.lowercase() ?: return null

        if (scheme == "data" || scheme == "blob") return false
        if (scheme != "http" && scheme != "https") return null

        if (isSameOrigin(url, saved) || isBundledLauncherOrigin(url)) {
            return false
        }

        // Keep untrusted origins outside the WebView so they cannot access the
        // Capacitor bridge. This may open SSO/admin links in the system browser;
        // QR-login remains the preferred mobile sign-in path.
        return try {
            context.startActivity(Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            true
        } catch (_: Exception) {
            true
        }
    }

    /**
     * Require HTTPS for confidential traffic. A bare host gets https://; plain
     * http:// is rejected except for local development hosts (emulator/loopback).
     */
    private fun normalize(input: String): String {
        var s = input
        val lower = s.lowercase()
        if (lower.startsWith("http://")) {
            val host = s.substringAfter("://").substringBefore("/").substringBefore(":")
            if (host !in LOCAL_HOSTS) {
                throw IllegalArgumentException("Только https-адрес (http запрещён)")
            }
        } else if (!lower.startsWith("https://")) {
            s = "https://$s"
        }
        return s.trimEnd('/')
    }

    companion object {
        const val PREFS = "server_config"
        const val KEY_URL = "url"
        const val KEY_PENDING_PATH = "pending_path"
        private val LOCAL_HOSTS = setOf("localhost", "127.0.0.1", "10.0.2.2")

        fun savedUrl(context: Context): String? =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, null)

        fun setPendingPath(context: Context, path: String) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(KEY_PENDING_PATH, path).apply()
        }

        fun clearMobileState(context: Context) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .remove(KEY_URL)
                .remove(KEY_PENDING_PATH)
                .apply()
            context.getSharedPreferences(AidocsPushPlugin.PREFS, Context.MODE_PRIVATE)
                .edit()
                .clear()
                .apply()
            context.stopService(Intent(context, AidocsPushService::class.java))
            CookieManager.getInstance().removeAllCookies(null)
            CookieManager.getInstance().flush()
        }

        private fun isSameOrigin(url: Uri, savedUrl: String): Boolean {
            val saved = Uri.parse(savedUrl)
            return url.scheme.equals(saved.scheme, ignoreCase = true) &&
                url.host.equals(saved.host, ignoreCase = true) &&
                normalizedPort(url) == normalizedPort(saved)
        }

        private fun isBundledLauncherOrigin(url: Uri): Boolean {
            return url.scheme.equals("https", ignoreCase = true) &&
                url.host.equals("localhost", ignoreCase = true)
        }

        private fun normalizedPort(uri: Uri): Int {
            if (uri.port != -1) return uri.port
            return when (uri.scheme?.lowercase()) {
                "http" -> 80
                "https" -> 443
                else -> -1
            }
        }
    }
}
