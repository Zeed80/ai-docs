import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import "./globals.css";
import { KeyboardProvider } from "@/lib/keyboard-context";
import { ClientLayout } from "@/components/ui/client-layout";

export const metadata: Metadata = {
  title: "AI Документооборот",
  description: "AI-powered document processing workspace",
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
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
