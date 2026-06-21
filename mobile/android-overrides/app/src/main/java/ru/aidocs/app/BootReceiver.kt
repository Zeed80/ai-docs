package ru.aidocs.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Restart the ntfy subscription service after a reboot (if push was configured). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            AidocsPushPlugin.startService(context)
        }
    }
}
