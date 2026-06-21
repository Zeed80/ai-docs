package ru.aidocs.app

import android.content.Context
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
        prefs().edit().putString(KEY_URL, url).apply()
        val res = JSObject()
        res.put("url", url)
        call.resolve(res)
    }

    @PluginMethod
    fun clear(call: PluginCall) {
        prefs().edit().remove(KEY_URL).apply()
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
    }
}
