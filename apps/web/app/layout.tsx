import type { Metadata } from "next";
import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "./globals.css";
import { Providers } from "@/components/providers";

const appName = process.env.APP_NAME ?? "Jhin";

export const metadata: Metadata = {
  title: appName,
  description: `${appName} — self-hosted platform for autonomous AI agent organizations`,
};

/** No-flash theme init: honors localStorage["jhin-theme"], else the OS preference. */
const themeInit = `(function(){try{var t=localStorage.getItem("jhin-theme");var d=t?t==="dark":window.matchMedia("(prefers-color-scheme: dark)").matches;document.documentElement.classList.toggle("dark",d)}catch(e){}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body className="min-h-full flex flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
