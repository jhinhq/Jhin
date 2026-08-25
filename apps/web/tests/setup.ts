/** Node >= 22 ships an experimental `localStorage` accessor on globalThis that
 * stays undefined unless `--localstorage-file` is set. vitest's jsdom
 * environment aliases `window` to the global object and skips keys that
 * already exist, so both `localStorage` and `window.localStorage` end up
 * undefined. Install a small in-memory Storage for tests instead. */

class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

for (const key of ["localStorage", "sessionStorage"] as const) {
  const existing = (globalThis as Record<string, unknown>)[key];
  if (existing === undefined || typeof (existing as Storage).getItem !== "function") {
    Object.defineProperty(globalThis, key, {
      value: new MemoryStorage(),
      configurable: true,
      writable: true,
    });
  }
}

/** jsdom implements neither `IntersectionObserver` (scroll-spy) nor
 * `Element.prototype.scrollIntoView` (jump-to-section). Install inert stand-ins
 * so components that reach for them render without throwing under test. */
if (typeof (globalThis as Record<string, unknown>).IntersectionObserver === "undefined") {
  class NoopIntersectionObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
    readonly root = null;
    readonly rootMargin = "";
    readonly thresholds = [];
  }
  Object.defineProperty(globalThis, "IntersectionObserver", {
    value: NoopIntersectionObserver,
    configurable: true,
    writable: true,
  });
}

if (typeof Element !== "undefined" && typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
