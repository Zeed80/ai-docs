import type { Metadata, Viewport } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import "./globals.css";
import { KeyboardProvider } from "@/lib/keyboard-context";
import { ClientLayout } from "@/components/ui/client-layout";
import {
  ServiceWorkerRegistration,
  InstallPrompt,
} from "@/components/pwa/ServiceWorkerRegistration";
import { OfflineQueueWidget } from "@/components/pwa/OfflineQueueWidget";
import { MobileChrome } from "@/components/mobile/MobileChrome";

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
          <KeyboardProvider>
            <ClientLayout>{children}</ClientLayout>
          </KeyboardProvider>
          <ServiceWorkerRegistration />
          <InstallPrompt />
          <OfflineQueueWidget />
          <MobileChrome />
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
