package ru.aidocs.app

import android.Manifest
import android.app.DownloadManager
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.util.Base64
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.MimeTypeMap
import android.webkit.URLUtil
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.widget.Toast
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
        wireDownloads(b.webView)
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

    /**
     * A WebView has no download handling of its own, so file "download" links do
     * nothing. The frontend exports files as blob: URLs (createObjectURL + <a
     * download>). We read the blob's bytes via JS → base64 → save into the public
     * Downloads folder (MediaStore, no storage permission needed on Android 10+).
     * Plain http(s) links go through the system DownloadManager, forwarding the
     * session cookie so authenticated downloads work.
     */
    private fun wireDownloads(webView: WebView) {
        webView.addJavascriptInterface(BlobDownloader(), "AidocsBlob")
        webView.setDownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            try {
                if (url.startsWith("blob:")) {
                    webView.evaluateJavascript(blobReaderJs(url), null)
                    return@setDownloadListener
                }
                val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
                val req = DownloadManager.Request(Uri.parse(url)).apply {
                    setMimeType(mimeType)
                    if (!userAgent.isNullOrEmpty()) addRequestHeader("User-Agent", userAgent)
                    CookieManager.getInstance().getCookie(url)?.let { addRequestHeader("cookie", it) }
                    setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
                }
                (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
                toast("Скачивание: $fileName")
            } catch (e: Exception) {
                toast("Не удалось скачать: ${e.message}")
            }
        }
    }

    /** Fetch a blob: URL in the page and hand its bytes back as a data URL. */
    private fun blobReaderJs(url: String): String =
        """
        (function(){
          try {
            var x = new XMLHttpRequest();
            x.open('GET', '$url', true);
            x.responseType = 'blob';
            x.onload = function(){
              var r = new FileReader();
              r.onloadend = function(){ AidocsBlob.save(r.result); };
              r.readAsDataURL(x.response);
            };
            x.send();
          } catch(e) {}
        })();
        """.trimIndent()

    /** Receives a `data:<mime>;base64,…` string from the page and saves it. */
    inner class BlobDownloader {
        @JavascriptInterface
        fun save(dataUrl: String) {
            try {
                val comma = dataUrl.indexOf(',')
                if (comma < 0) return
                val meta = dataUrl.substring(5, comma) // "<mime>;base64"
                val mime = meta.substringBefore(';').ifEmpty { "application/octet-stream" }
                val bytes = Base64.decode(dataUrl.substring(comma + 1), Base64.DEFAULT)
                val ext = MimeTypeMap.getSingleton().getExtensionFromMimeType(mime) ?: "bin"
                val name = "aidocs_${System.currentTimeMillis()}.$ext"
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    val values = ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, name)
                        put(MediaStore.Downloads.MIME_TYPE, mime)
                        put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                    }
                    val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    uri?.let { contentResolver.openOutputStream(it)?.use { os -> os.write(bytes) } }
                } else {
                    val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
                    dir.mkdirs()
                    java.io.File(dir, name).outputStream().use { it.write(bytes) }
                }
                toast("Сохранено в Загрузки: $name")
            } catch (e: Exception) {
                toast("Не удалось сохранить файл: ${e.message}")
            }
        }
    }

    private fun toast(msg: String) {
        runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_SHORT).show() }
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
