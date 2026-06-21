package ru.aiworkspace.sveta

import android.content.Intent
import android.os.Bundle
import com.getcapacitor.BridgeActivity

/**
 * Main activity for the Света shell.
 *
 * Registers the app-owned Capacitor plugins (ServerConfig, AppUpdate, SvetaPush).
 * The community SendIntent plugin handles ACTION_SEND/SEND_MULTIPLE intake on its
 * own, so file sharing needs no custom code here.
 *
 * A push notification tap arrives with our internal action path; we forward it to
 * the WebView so the web app routes to action_url.
 */
class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        registerPlugin(ServerConfigPlugin::class.java)
        registerPlugin(AppUpdatePlugin::class.java)
        registerPlugin(SvetaPushPlugin::class.java)
        super.onCreate(savedInstanceState)
        handleDeepLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    /** Navigate the WebView to a relative path carried by a notification tap. */
    private fun handleDeepLink(intent: Intent?) {
        val path = intent?.getStringExtra(EXTRA_ACTION_PATH) ?: return
        if (path.startsWith("/")) {
            bridge?.webView?.post {
                bridge?.eval(
                    "window.location.assign(${jsString(path)})",
                    null,
                )
            }
        }
    }

    private fun jsString(s: String): String =
        "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    companion object {
        const val EXTRA_ACTION_PATH = "sveta_action_path"
    }
}
