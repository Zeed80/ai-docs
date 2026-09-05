import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import "./globals.css";
import { KeyboardProvider } from "@/lib/keyboard-context";
import { ClientLayout } from "@/components/ui/client-layout";
import { ToastProvider } from "@/components/ui/primitives/Toast";
import {
  ServiceWorkerRegistration,
  InstallPrompt,
} from "@/components/pwa/ServiceWorkerRegistration";
import { OfflineQueueWidget } from "@/components/pwa/OfflineQueueWidget";
import { MobileChrome } from "@/components/mobile/MobileChrome";
import { CommandPalette } from "@/components/command-palette";

export const metadata: Metadata = {
  title: "AI-DOCS",
  description: "AI-powered document processing workspace",
  manifest: "/manifest.json",
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "AI-DOCS",
  },
};

export const viewport: Viewport = {
  themeColor: "#1e293b",
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const locale = await getLocale();
  const messages = await getMessages();

  return (
    <html lang={locale}>
      <body className="bg-slate-900 text-slate-100 antialiased">
        <NextIntlClientProvider messages={messages}>
          {/* Провайдер сообщений на весь сайт: до этого об ошибке запроса
              разделы либо молчали (`if (r.ok)` без ветки else), либо звали
              alert() браузера. Теперь сообщить может любой экран. */}
          <ToastProvider>
            <KeyboardProvider>
              <ClientLayout>{children}</ClientLayout>
            </KeyboardProvider>
          </ToastProvider>
          {/* Ctrl/⌘+K из любого места: поиск и переходы были разбросаны по
              разделам, хотя проект заявляет keyboard-first. */}
          <CommandPalette />
          <ServiceWorkerRegistration />
          <InstallPrompt />
          <OfflineQueueWidget />
          <MobileChrome />
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
