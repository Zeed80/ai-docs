package ru.aidocs.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.getcapacitor.BridgeActivity

/**
 * Main activity for the Света shell.
 *
 * Registers the app-owned Capacitor plugins (ServerConfig, AppUpdate, AidocsPush).
 * The community SendIntent plugin handles ACTION_SEND/SEND_MULTIPLE intake on its
 * own, so file sharing needs no custom code here.
 *
 * Security: FLAG_SECURE blocks screenshots and hides the app content in the
 * recent-apps preview (the app shows confidential documents).
 *
 * A push notification tap carries our internal action path; we forward it to the
 * WebView. If the app is cold-started by the tap (WebView still on the launcher),
 * the path is stashed and applied by the web layer once the server has loaded.
 */
class MainActivity : BridgeActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        registerPlugin(ServerConfigPlugin::class.java)
        registerPlugin(AppUpdatePlugin::class.java)
        registerPlugin(AidocsPushPlugin::class.java)
        super.onCreate(savedInstanceState)

        // Confidential content — block screenshots + recent-apps thumbnail.
        window.setFlags(
            WindowManager.LayoutParams.FLAG_SECURE,
            WindowManager.LayoutParams.FLAG_SECURE,
        )

        requestNotificationPermission()
        handleDeepLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    /** Android 13+ requires a runtime grant for notifications to be shown. */
    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1001,
            )
        }
    }

    /** Navigate the WebView to a relative path carried by a notification tap. */
    private fun handleDeepLink(intent: Intent?) {
        val path = intent?.getStringExtra(EXTRA_ACTION_PATH) ?: return
        if (!path.startsWith("/")) return
        // If the server is already loaded, navigate now; otherwise stash it so the
        // web layer can pick it up after the live site loads (cold start).
        if (ServerConfigPlugin.savedUrl(this) != null && bridge?.webView?.url?.startsWith("http") == true) {
            bridge?.webView?.post {
                bridge?.eval("window.location.assign(${jsString(path)})", null)
            }
        } else {
            ServerConfigPlugin.setPendingPath(this, path)
        }
    }

    private fun jsString(s: String): String =
        "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

    companion object {
        const val EXTRA_ACTION_PATH = "aidocs_action_path"
    }
}
