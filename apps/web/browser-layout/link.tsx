import type { AnchorHTMLAttributes } from "react";

// Next Link requires Next's router runtime; this fixture only measures its anchor.
export default function Link(props: AnchorHTMLAttributes<HTMLAnchorElement>) {
  return <a {...props} />;
}
