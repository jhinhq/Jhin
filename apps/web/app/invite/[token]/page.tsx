import { placeholderParams } from "@/lib/route-params";
import AcceptInvitePage from "./view";

/**
 * Static export builds one page per dynamic route, not one per id: the
 * desktop shell serves this placeholder for every `invite/<token>` URL and
 * the view reads the real token from the pathname. The server build is
 * unaffected — `dynamicParams` defaults to true, so any other token still
 * renders on demand.
 */
export function generateStaticParams() {
  return placeholderParams("token");
}

export default function Page() {
  return <AcceptInvitePage />;
}
