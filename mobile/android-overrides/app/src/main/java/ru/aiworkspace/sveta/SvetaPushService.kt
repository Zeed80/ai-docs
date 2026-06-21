package ru.aiworkspace.sveta

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Foreground service holding a persistent connection to ntfy (no Google services).
 *
 * Streams the per-device topic via ntfy's newline-delimited JSON endpoint
 * (GET {url}/{topic}/json) and posts a system notification for each message.
 * Reconnects with backoff. A low-importance "ongoing" notification keeps the
 * service alive on Android 8+.
 */
class SvetaPushService : Service() {

    private val running = AtomicBoolean(false)
    @Volatile private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val url = intent?.getStringExtra("url")
            ?: prefs().getString(SvetaPushPlugin.KEY_URL, null)
        val topic = intent?.getStringExtra("topic")
            ?: prefs().getString(SvetaPushPlugin.KEY_TOPIC, null)

        createChannels()
        startForeground(ONGOING_ID, ongoingNotification())

        if (url != null && topic != null && running.compareAndSet(false, true)) {
            worker = Thread { subscribeLoop(url.trimEnd('/'), topic) }.also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running.set(false)
        worker?.interrupt()
        super.onDestroy()
    }

    private fun prefs() =
        getSharedPreferences(SvetaPushPlugin.PREFS, Context.MODE_PRIVATE)

    private fun subscribeLoop(url: String, topic: String) {
        var backoff = 2000L
        while (running.get()) {
            try {
                val conn = (URL("$url/$topic/json").openConnection() as HttpURLConnection).apply {
                    connectTimeout = 15000
                    readTimeout = 0 // stream indefinitely
                    setRequestProperty("Accept", "application/x-ndjson")
                }
                conn.inputStream.bufferedReader().use { reader ->
                    backoff = 2000L
                    while (running.get()) {
                        val line = reader.readLine() ?: break
                        if (line.isBlank()) continue
                        handleMessage(line)
                    }
                }
            } catch (_: InterruptedException) {
                return
            } catch (_: Exception) {
                // fall through to backoff/reconnect
            }
            if (!running.get()) return
            try {
                Thread.sleep(backoff)
            } catch (_: InterruptedException) {
                return
            }
            backoff = (backoff * 2).coerceAtMost(60000L)
        }
    }

    private fun handleMessage(line: String) {
        try {
            val obj = JSONObject(line)
            // ntfy control frames (event=open/keepalive) carry no message.
            if (obj.optString("event") != "message" && !obj.has("message")) return
            val title = obj.optString("title").ifEmpty { "Света" }
            val body = obj.optString("message")
            val click = obj.optString("click") // absolute action_url
            postUserNotification(title, body, click)
        } catch (_: Exception) {
            // ignore malformed frames
        }
    }

    private fun postUserNotification(title: String, body: String, click: String) {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
            if (click.isNotEmpty()) {
                putExtra(MainActivity.EXTRA_ACTION_PATH, relativePath(click))
            }
        }
        val pi = PendingIntent.getActivity(
            this, click.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val n = NotificationCompat.Builder(this, CHANNEL_MESSAGES)
            .setSmallIcon(android.R.drawable.ic_dialog_email)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(pi)
            .build()
        manager().notify(System.currentTimeMillis().toInt(), n)
    }

    /** Reduce an absolute action_url to a site-relative path for the WebView. */
    private fun relativePath(url: String): String = try {
        val u = URL(url)
        (u.path + (if (u.query != null) "?" + u.query else "")).ifEmpty { "/" }
    } catch (_: Exception) {
        if (url.startsWith("/")) url else "/"
    }

    private fun ongoingNotification(): Notification =
        NotificationCompat.Builder(this, CHANNEL_ONGOING)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("Света")
            .setContentText("Уведомления включены")
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setOngoing(true)
            .build()

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = manager()
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL_ONGOING, "Фоновая служба", NotificationManager.IMPORTANCE_MIN),
        )
        mgr.createNotificationChannel(
            NotificationChannel(CHANNEL_MESSAGES, "Уведомления", NotificationManager.IMPORTANCE_HIGH),
        )
    }

    private fun manager() =
        getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        const val ONGOING_ID = 1001
        const val CHANNEL_ONGOING = "sveta_ongoing"
        const val CHANNEL_MESSAGES = "sveta_messages"
    }
}
