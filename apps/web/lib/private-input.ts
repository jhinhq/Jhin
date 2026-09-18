/** Conservative browser persistence guard. Server intake remains authoritative. */
export function mayContainSecret(text: string): boolean {
  return /\b[a-f\d]{24}:[a-f\d]{64}\b/i.test(text)
    || /\b(?:api[ _-]?key|admin[ _-]?key|secret[ _-]?key|access[ _-]?token|client[ _-]?secret|auth[ _-]?token|password|passphrase|private[ _-]?key|x-api-key)\b\s*(?:is\b|[=:])/i.test(text)
    || /\bbearer\s+\S+/i.test(text)
    || /\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]+|sb_secret_[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b/.test(text)
    || /\b[a-z][a-z0-9+.-]*:\/\/[^/\s:@]+:[^/\s@]+@/i.test(text)
    || /-----BEGIN [A-Z ]*PRIVATE KEY-----/.test(text);
}
export interface SecureChatInput { name?: string; value: string; }
