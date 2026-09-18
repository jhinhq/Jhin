/** Build-time rollback switch. Existing transcripts and files remain stored. */
export const AGENTIC_WORKSPACE_ENABLED =
  process.env.NEXT_PUBLIC_AGENTIC_WORKSPACE !== "0" &&
  process.env.NEXT_PUBLIC_AGENTIC_WORKSPACE !== "false";
