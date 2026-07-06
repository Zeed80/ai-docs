package ru.aidocs.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.webkit.WebViewFeature
import com.getcapacitor.BridgeActivity
import com.getcapacitor.BridgeWebViewClient
import com.getcapacitor.CapConfig
import java.net.URL

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

        // Load the runtime-selected server AS the Capacitor server so the native
        // bridge is injected. A site opened as a "foreign" origin (via plain
        // navigation / allowNavigation) gets NO bridge → no camera, biometrics,
        // push. The URL still comes from ServerConfig (chosen at first launch or
        // by QR), not baked at build time. Must be set before super.onCreate,
        // which calls load() and reads this.config.
        val saved = ServerConfigPlugin.savedUrl(this)
        if (!saved.isNullOrEmpty()) {
            val builder = CapConfig.Builder(this)
                .setServerUrl(saved)
                .setAndroidScheme("https")
                .setAllowNavigation(arrayOf("*"))
            // A pending path (login-QR redeem stashed by the launcher, or a push
            // deep link) becomes the initial route → land signed-in / on target.
            val pending = ServerConfigPlugin.consumePendingPathValue(this)
            if (!pending.isNullOrEmpty()) builder.setStartPath(pending)
            this.config = builder.create()
        }

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

    /**
     * With server.url set, Capacitor routes EVERY request to that host through its
     * local-server proxy (re-fetches server-side), which breaks cookies/session,
     * file downloads and streaming (SSE). The native bridge, however, is injected
     * separately via addDocumentStartJavaScript (modern WebViews), independent of
     * that proxy. So we swap in a WebViewClient that bypasses the proxy for our own
     * server — the site then loads directly (everything works) while the bridge
     * (camera/biometrics/push) stays injected. Gated on DOCUMENT_START_SCRIPT: if
     * unsupported (old WebView) the bridge would come from the proxy, so we leave
     * the proxy in place (no regression). Requires one reload so the initial page
     * (already started via the proxy) re-fetches directly.
     */
    override fun load() {
        super.load()
        val b = bridge ?: return
        val host = ServerConfigPlugin.savedUrl(this)
            ?.let { runCatching { URL(it).host }.getOrNull() }
            ?: return
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return

        b.setWebViewClient(object : BridgeWebViewClient(b) {
            override fun shouldInterceptRequest(
                view: WebView,
                request: WebResourceRequest,
            ): WebResourceResponse? {
                if (request.url.host?.equals(host, ignoreCase = true) == true) {
                    return null // load our server directly, not through the proxy
                }
                return super.shouldInterceptRequest(view, request)
            }
        })
        b.webView.post { b.webView.reload() }
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
