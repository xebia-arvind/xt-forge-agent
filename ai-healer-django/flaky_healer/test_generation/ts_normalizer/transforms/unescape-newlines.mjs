/**
 * Unescape literal `\n`, `\t`, `\r\n` character sequences that some LLM
 * responses emit as backslash-n rather than actual newlines. TypeScript
 * rejects those with `TS1127 Invalid character`.
 *
 * Idempotent — nothing to unescape after the first pass.
 */
export function unescapeLiteralNewlines(content) {
  if (!content) return { changed: false, content };
  const before = content;
  let out = content;
  // Order matters: \r\n first so we don't leave an orphan \r.
  out = out.replace(/\\r\\n/g, "\n")
           .replace(/\\n/g, "\n")
           .replace(/\\t/g, "  ")
           .replace(/\\r/g, "");
  if (out === before) return { changed: false, content };

  // Rough count of the sequences we replaced so the report has a number.
  const converted =
      (before.match(/\\r\\n|\\n|\\t|\\r/g) || []).length;

  return {
    changed: true,
    content: out,
    report: {
      name: "unescape-literal-newlines",
      detail: `converted ${converted} backslash-escape sequence(s) to real whitespace`,
    },
  };
}
