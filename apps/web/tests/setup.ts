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
