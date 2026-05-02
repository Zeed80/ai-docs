import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages } from "next-intl/server";
import "./globals.css";
import { Sidebar } from "@/components/ui/sidebar";
import { KeyboardProvider } from "@/lib/keyboard-context";
import { SvetaPanel } from "@/components/chat/sveta-panel";
import { ResizableLayout } from "@/components/ui/resizable-layout";

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
            <ResizableLayout sidebar={<Sidebar />} chat={<SvetaPanel />}>
              {children}
            </ResizableLayout>
          </KeyboardProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
