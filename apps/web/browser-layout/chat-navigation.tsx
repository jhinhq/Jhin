/** Only the Next router boundary is substituted; the page and data hooks are real. */
export function usePathname() {
  return "/chats/browser-chat";
}
export function useRouter() {
  return { push: (path: string) => window.history.pushState({}, "", path) };
}
