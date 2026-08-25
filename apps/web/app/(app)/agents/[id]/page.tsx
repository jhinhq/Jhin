import { placeholderParams } from "@/lib/route-params";
import AgentProfilePage from "./view";

/**
 * Static export builds one page per dynamic route, not one per id: the
 * desktop shell serves this placeholder for every `agents/<id>` URL and
 * the view reads the real id from the pathname. The server build is
 * unaffected — `dynamicParams` defaults to true, so any other id still
 * renders on demand.
 */
export function generateStaticParams() {
  return placeholderParams("id");
}

export default function Page() {
  return <AgentProfilePage />;
}
