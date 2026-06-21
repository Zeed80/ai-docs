package ru.aiworkspace.sveta

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
        val url = normalize(raw)
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

    /** Add https:// when no scheme is given; strip a trailing slash. */
    private fun normalize(input: String): String {
        var s = input
        if (!s.startsWith("http://") && !s.startsWith("https://")) s = "https://$s"
        return s.trimEnd('/')
    }

    companion object {
        const val PREFS = "server_config"
        const val KEY_URL = "url"

        fun savedUrl(context: Context): String? =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, null)
    }
}
