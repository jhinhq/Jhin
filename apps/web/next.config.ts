import type { NextConfig } from "next";

// Baked into the build (Docker sets API_INTERNAL_URL=http://api:8000).
// Browser calls go same-origin to /api/* so auth cookies never cross origins.
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const isProduction = process.env.NODE_ENV === "production";

/**
 * Content-Security-Policy for the app shell.
 *
 * `script-src 'unsafe-inline'` is a deliberate, documented compromise, not an
 * oversight: the App Router streams its payload through inline
 * `self.__next_f.push(...)` scripts whose content changes every build, and
 * `app/layout.tsx` carries an inline theme bootstrap that must run before
 * first paint to avoid a flash. Hashing them is not stable across builds, and
 * nonces require middleware that forces every route to render dynamically.
 * Everything else is locked down — in particular `default-src 'self'`,
 * `object-src 'none'`, `frame-ancestors 'none'` and `base-uri 'self'`, which
 * are what stop framing, plugin abuse, and `<base>` hijacking. Fonts are
 * self-hosted (@fontsource), so no external origin is needed anywhere.
 *
 * Tightening `script-src` with per-request nonces is tracked in
 * docs/security-assessment.md as accepted residual risk.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "form-action 'self'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "media-src 'self' data: blob:",
  "worker-src 'self' blob:",
  "manifest-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  // Dev needs 'unsafe-eval' for React Fast Refresh and ws: for HMR.
  `script-src 'self' 'unsafe-inline'${isProduction ? "" : " 'unsafe-eval'"}`,
  `connect-src 'self'${isProduction ? "" : " ws: wss:"}`,
].join("; ");

const permissionsPolicy = [
  "accelerometer=()",
  "autoplay=()",
  "camera=()",
  "display-capture=()",
  "encrypted-media=()",
  "geolocation=()",
  "gyroscope=()",
  "magnetometer=()",
  "microphone=()",
  "midi=()",
  "payment=()",
  "publickey-credentials-get=()",
  "screen-wake-lock=()",
  "usb=()",
  "xr-spatial-tracking=()",
].join(", ");

const baseSecurityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: permissionsPolicy },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

// Browsers ignore HSTS on plaintext responses, so emitting it from a
// production build that happens to be served over http://localhost is inert.
const productionOnlyHeaders = isProduction
  ? [
      {
        key: "Strict-Transport-Security",
        value: "max-age=63072000; includeSubDomains",
      },
    ]
  : [];

/**
 * The desktop shell (apps/desktop) ships this app as a static bundle and does
 * the three server-side jobs itself: it serves the files, applies the security
 * headers below, and proxies `/api/*` to whichever Jhin instance the user
 * connected to — injecting their API key on the way through, which is why the
 * key never has to exist inside this bundle's JavaScript.
 *
 * Static export supports none of `headers`, `redirects`, or `rewrites`, so the
 * export build drops them and `apps/desktop/src/server.rs` reproduces each one.
 * The two must stay in step; `apps/desktop/tests` pins the header set.
 */
const isDesktopExport = process.env.JHIN_DESKTOP === "1";

const nextConfig: NextConfig = isDesktopExport
  ? {
      output: "export",
      poweredByHeader: false,
      // Deep routes become directories with an index.html, which is what the
      // shell's file lookup expects.
      trailingSlash: true,
      images: { unoptimized: true },
    }
  : {
  // Self-contained server bundle consumed by the Docker runtime stage.
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [...baseSecurityHeaders, ...productionOnlyHeaders],
      },
      {
        // The API sets its own, much stricter, `default-src 'none'` policy on
        // /api/* responses; applying the document policy there too would just
        // stack two headers on a JSON body.
        source: "/((?!api/).*)",
        headers: [{ key: "Content-Security-Policy", value: contentSecurityPolicy }],
      },
    ];
  },
  // Real HTTP redirects, resolved before routing so deep links and bookmarks
  // never pay for a client-side bounce. `/connectors`, `/triggers`, and
  // `/organization` are permanent: Apps absorbed the first, Automations the
  // second, and Company the third. `/` is temporary: it is only the landing
  // choice.
  async redirects() {
    return [
      { source: "/", destination: "/home", permanent: false },
      { source: "/connectors", destination: "/apps", permanent: true },
      { source: "/triggers", destination: "/automations", permanent: true },
      { source: "/organization", destination: "/company", permanent: true },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiInternalUrl}/api/:path*`,
      },
    ];
  },
};

export { contentSecurityPolicy, permissionsPolicy };
export default nextConfig;
