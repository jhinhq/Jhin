/** Pure helpers behind the OAuth connect flow: the return route the browser
 * keeps across a provider round trip, the device code as people read it, and
 * the closed vocabulary the callback's error codes turn into. */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  consumeReturnRoute,
  credentialSchemes,
  describePermissions,
  describeScopes,
  devicePollDelayMs,
  formatCountdown,
  formatUserCode,
  needsReauth,
  oauthErrorMessage,
  postFormTo,
  readGitHubAppLanding,
  safeHttpsUrl,
  saveReturnRoute,
  secondsUntil,
  SLOW_DOWN_STEP_MS,
} from "@/lib/oauth";

const KEY = "jhin.oauth.return";

/** Swap sessionStorage for one that throws on every access, the way a
 * hardened browser or a private window does. */
function withBrokenStorage(run: () => void): void {
  const original = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    get() {
      throw new DOMException("The operation is insecure.", "SecurityError");
    },
  });
  try {
    run();
  } finally {
    if (original) Object.defineProperty(globalThis, "sessionStorage", original);
  }
}

afterEach(() => {
  try {
    window.sessionStorage.clear();
  } catch {
    /* the broken-storage test restores its own descriptor */
  }
  vi.restoreAllMocks();
});

describe("the return route", () => {
  it("round-trips the route the browser left from", () => {
    saveReturnRoute("/agents/abc?tab=tools");
    expect(consumeReturnRoute()).toBe("/agents/abc?tab=tools");
  });

  it("is consumed exactly once", () => {
    saveReturnRoute("/apps");
    expect(consumeReturnRoute()).toBe("/apps");
    expect(consumeReturnRoute()).toBeNull();
  });

  it("defaults to where the browser currently is", () => {
    saveReturnRoute();
    expect(consumeReturnRoute()).toBe(`${window.location.pathname}${window.location.search}`);
  });

  it("refuses to store anything that is not a same-origin path", () => {
    for (const hostile of [
      "https://evil.example/steal",
      "//evil.example",
      "/\\evil.example",
      "javascript:alert(1)",
      "",
    ]) {
      saveReturnRoute(hostile);
      expect(consumeReturnRoute()).toBeNull();
    }
  });

  it("refuses a hostile value planted in storage directly", () => {
    // sessionStorage is writable by any script on this origin, so the guard
    // has to run on the way out, not only on the way in.
    for (const planted of ["//evil.example", "https://evil.example", "/apps\\@evil.example"]) {
      window.sessionStorage.setItem(KEY, planted);
      expect(consumeReturnRoute()).toBeNull();
    }
  });

  it("costs the return trip, never the connection, when storage throws", () => {
    withBrokenStorage(() => {
      expect(() => saveReturnRoute("/apps")).not.toThrow();
      expect(consumeReturnRoute()).toBeNull();
    });
  });
});

describe("formatUserCode", () => {
  it("groups eight bare characters", () => {
    expect(formatUserCode("wdjbmjht")).toBe("WDJB-MJHT");
  });

  it("is idempotent on a code that already arrived grouped", () => {
    expect(formatUserCode("WDJB-MJHT")).toBe("WDJB-MJHT");
    expect(formatUserCode(formatUserCode("wdjbmjht"))).toBe("WDJB-MJHT");
  });

  it("leaves an unfamiliar shape alone rather than mangling it", () => {
    expect(formatUserCode("ABC123")).toBe("ABC123");
    expect(formatUserCode(" 12345 678 ")).toBe("12345 678");
  });
});

describe("describeScopes", () => {
  it("reads as a sentence", () => {
    expect(describeScopes(["read"])).toBe("read");
    expect(describeScopes(["read", "write"])).toBe("read and write");
    expect(describeScopes(["read", "write", "issues:create"])).toBe(
      "read, write, and issues:create",
    );
  });

  it("is honest about an empty list instead of inventing one", () => {
    expect(describeScopes([])).toBe("the access this app needs by default");
    expect(describeScopes(["  ", ""])).toBe("the access this app needs by default");
  });
});

describe("devicePollDelayMs", () => {
  it("stops polling on every terminal status", () => {
    for (const status of ["connected", "denied", "expired"]) {
      expect(devicePollDelayMs(5000, status, 5, 0)).toBe(false);
    }
  });

  it("treats the server's interval as a floor, never a ceiling", () => {
    expect(devicePollDelayMs(5000, "pending", 10, 0)).toBe(10_000);
    expect(devicePollDelayMs(5000, "pending", 1, 0)).toBe(5000);
  });

  it("adds the earned backoff and never gives it back", () => {
    expect(devicePollDelayMs(5000, "slow_down", 5, SLOW_DOWN_STEP_MS)).toBe(10_000);
    // A later `pending` carrying the original interval must not undo it.
    expect(devicePollDelayMs(5000, "pending", 5, SLOW_DOWN_STEP_MS)).toBe(10_000);
  });
});

describe("oauthErrorMessage", () => {
  it("has a closed vocabulary and never echoes the provider", () => {
    expect(oauthErrorMessage(null)).toBeNull();
    expect(oauthErrorMessage("denied")).toContain("declined");
    expect(oauthErrorMessage("failed")).toContain("could not be completed");
    // Anything unrecognised — including text a provider influenced — collapses
    // to the same Jhin-authored sentence.
    expect(oauthErrorMessage("<script>alert(1)</script>")).toBe(oauthErrorMessage("failed"));
  });

  it("names the two first-setup mistakes a person can fix", () => {
    const rejected = oauthErrorMessage("client_rejected");
    expect(rejected).toContain("did not accept this app's client id and secret");
    expect(rejected).toContain("Settings → OAuth");
    expect(rejected).toContain("Apps → Connect GitHub");
    const mismatch = oauthErrorMessage("callback_mismatch");
    expect(mismatch).toContain("callback URL listed on the app is not this instance's redirect URL");
    expect(mismatch).toContain("Settings → OAuth");
  });
});

describe("credentialSchemes", () => {
  it("drops the sign-in schemes and keeps every one a person can fill in", () => {
    const connector = {
      auth_schemes: [
        { type: "oauth" },
        { type: "device" },
        { type: "device_code" },
        { type: "none" },
        { type: "bearer" },
        { type: "pat" },
      ],
    };
    expect(credentialSchemes(connector).map((scheme) => scheme.type)).toEqual([
      "none",
      "bearer",
      "pat",
    ]);
    expect(credentialSchemes({ auth_schemes: [{ type: "oauth" }] })).toEqual([]);
  });
});

describe("readGitHubAppLanding", () => {
  it("maps the manifest callback's flag and GitHub's install return", () => {
    expect(readGitHubAppLanding(new URLSearchParams("github_app=created"))).toBe("created");
    expect(readGitHubAppLanding(new URLSearchParams("github_app=failed"))).toBe("failed");
    expect(
      readGitHubAppLanding(new URLSearchParams("installation_id=123&setup_action=install")),
    ).toBe("installed");
    expect(readGitHubAppLanding(new URLSearchParams("setup_action=update"))).toBe("installed");
  });

  it("ignores anything outside the closed sets, and never reads the installation id", () => {
    expect(readGitHubAppLanding(new URLSearchParams(""))).toBeNull();
    expect(readGitHubAppLanding(new URLSearchParams("github_app=<script>"))).toBeNull();
    expect(readGitHubAppLanding(new URLSearchParams("setup_action=uninstall"))).toBeNull();
    expect(readGitHubAppLanding(new URLSearchParams("installation_id=123"))).toBeNull();
    // The manifest flag wins when both are present; the shape is the same.
    expect(
      readGitHubAppLanding(new URLSearchParams("github_app=created&setup_action=install")),
    ).toBe("created");
  });
});

describe("safeHttpsUrl", () => {
  it("passes https and refuses everything else", () => {
    expect(safeHttpsUrl("https://github.com/settings/apps")).toBe(
      "https://github.com/settings/apps",
    );
    for (const hostile of ["http://evil.example", "javascript:alert(1)", "not a url", "", null]) {
      expect(safeHttpsUrl(hostile)).toBeNull();
    }
    expect(safeHttpsUrl(undefined)).toBeNull();
  });
});

describe("describePermissions", () => {
  it("lists permissions the way GitHub's settings page spells them, in name order", () => {
    expect(
      describePermissions({
        pull_requests: "write",
        contents: "write",
        metadata: "read",
        checks: "read",
        actions: "write",
        issues: "write",
      }),
    ).toBe(
      "Actions (read & write), Checks (read), Contents (read & write), Issues (read & write), Metadata (read), Pull requests (read & write)",
    );
    expect(describePermissions({})).toBe("");
  });
});

describe("countdown formatting", () => {
  it("counts down to an instant and floors at zero", () => {
    const now = Date.parse("2026-08-29T12:00:00Z");
    expect(secondsUntil("2026-08-29T12:08:20Z", now)).toBe(500);
    expect(secondsUntil("2026-08-29T11:59:00Z", now)).toBe(0);
    expect(secondsUntil("not a date", now)).toBe(0);
  });

  it("renders m:ss", () => {
    expect(formatCountdown(500)).toBe("8:20");
    expect(formatCountdown(9)).toBe("0:09");
    expect(formatCountdown(-5)).toBe("0:00");
  });
});

describe("postFormTo", () => {
  it("posts the named fields and cleans up after itself", () => {
    const submitted: { action: string; fields: Record<string, string> }[] = [];
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(function submitSpy(this: HTMLFormElement) {
        const fields: Record<string, string> = {};
        for (const input of Array.from(this.querySelectorAll("input"))) {
          fields[input.name] = input.value;
        }
        submitted.push({ action: this.getAttribute("action") ?? "", fields });
      });

    postFormTo("https://github.com/settings/apps/new", {
      manifest: '{"name":"Jhin"}',
      state: "handle-123",
    });

    expect(submit).toHaveBeenCalledTimes(1);
    expect(submitted[0].action).toBe("https://github.com/settings/apps/new");
    expect(submitted[0].fields).toEqual({ manifest: '{"name":"Jhin"}', state: "handle-123" });
    expect(document.querySelectorAll("form")).toHaveLength(0);
  });
});

describe("needsReauth", () => {
  it("picks out the connections whose sign-in lapsed", () => {
    const rows = [
      { id: "a", status: "active" },
      { id: "b", status: "needs_reauth" },
      { id: "c", status: "error" },
      { id: "d", status: "active", needs_reauth: true },
    ];
    expect(needsReauth(rows).map((row) => row.id)).toEqual(["b", "d"]);
  });
});
