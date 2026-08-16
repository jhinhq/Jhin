import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/providers";

const appName = process.env.APP_NAME ?? "Jhin";

export const metadata: Metadata = {
  title: appName,
  description: `${appName} — self-hosted platform for autonomous AI agent organizations`,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
