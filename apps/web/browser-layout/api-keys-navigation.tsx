/** Fixture-only router boundary. Real pages/hooks are rendered in Vite. */
export { usePathname } from "./chat-navigation";

export function useRouter() {
  return { replace: () => {}, push: () => {}, refresh: () => {}, back: () => {} };
}
