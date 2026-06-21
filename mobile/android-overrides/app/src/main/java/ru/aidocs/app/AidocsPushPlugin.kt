package ru.aidocs.app

import android.content.Context
import android.content.Intent
import android.os.Build
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import java.util.UUID

/**
 * Owns the per-device ntfy topic and the foreground subscription service.
 *
 * register()  → returns a stable random topic (the secret). The web layer posts it
 *               to /api/devices/register so the backend can address pushes here.
 * configure() → persists the resolved external ntfy URL + topic and (re)starts the
 *               foreground subscription service.
 *
 * Privacy: only title/body/type/action_url ever flow through ntfy; topics are random.
 */
@CapacitorPlugin(name = "AidocsPush")
class AidocsPushPlugin : Plugin() {

    private fun prefs() = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun ensureTopic(): String {
        val existing = prefs().getString(KEY_TOPIC, null)
        if (existing != null) return existing
        val topic = "aidocs-" + UUID.randomUUID().toString().replace("-", "")
        prefs().edit().putString(KEY_TOPIC, topic).apply()
        return topic
    }

    @PluginMethod
    fun register(call: PluginCall) {
        val topic = ensureTopic()
        val res = JSObject()
        res.put("topic", topic)
        // We use a bare topic (the device subscribes directly), not a UnifiedPush
        // distributor endpoint, so endpoint is intentionally absent.
        call.resolve(res)
    }

    @PluginMethod
    fun configure(call: PluginCall) {
        val url = call.getString("url")
        val topic = call.getString("topic") ?: ensureTopic()
        if (url.isNullOrEmpty()) {
            call.reject("Missing ntfy url")
            return
        }
        prefs().edit()
            .putString(KEY_URL, url.trimEnd('/'))
            .putString(KEY_TOPIC, topic)
            .apply()
        startService(context)
        call.resolve()
    }

    @PluginMethod
    fun getTopic(call: PluginCall) {
        val res = JSObject()
        res.put("topic", ensureTopic())
        call.resolve(res)
    }

    companion object {
        const val PREFS = "aidocs_push"
        const val KEY_TOPIC = "topic"
        const val KEY_URL = "ntfy_url"

        fun startService(context: Context) {
            val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            val url = prefs.getString(KEY_URL, null) ?: return
            val topic = prefs.getString(KEY_TOPIC, null) ?: return
            val intent = Intent(context, AidocsPushService::class.java).apply {
                putExtra("url", url)
                putExtra("topic", topic)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }
}
