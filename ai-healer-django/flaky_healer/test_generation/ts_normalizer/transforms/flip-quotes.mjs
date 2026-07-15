/**
 * Fix `'[data-testid='foo']'` — outer `'` closes on the first inner `'`,
 * TypeScript then sees the rest as `TS1005: ',' expected` etc. Flip the
 * outer pair to the opposite quote so the inner quotes are preserved.
 *
 * Runs BEFORE ts-morph parses, because a file with this shape won't parse.
 */
export function flipInnerSameQuotes(content) {
  if (!content) return { changed: false, content };

  let flipped = 0;
  const lines = content.split("\n").map(line => {
    let l = line;
    for (const outer of ["'", '"']) {
      const other = outer === "'" ? '"' : "'";
      // Pattern: <outer><stuff-without-outer>=<outer><stuff-without-outer><outer><stuff-without-outer><outer>
      // Matches attribute-selector shapes like  '[key='value']'  → "[key='value']"
      const re = new RegExp(
        `${escapeReg(outer)}([^${escapeReg(outer)}\\n]*=${escapeReg(outer)}[^${escapeReg(outer)}\\n]*${escapeReg(outer)}[^${escapeReg(outer)}\\n]*)${escapeReg(outer)}`,
        "g",
      );
      l = l.replace(re, (_full, inner) => {
        if (inner.includes(other)) return _full;   // can't safely flip
        flipped += 1;
        return `${other}${inner}${other}`;
      });
    }
    return l;
  });

  if (flipped === 0) return { changed: false, content };
  return {
    changed: true,
    content: lines.join("\n"),
    report: {
      name: "flip-inner-same-quotes",
      detail: `converted ${flipped} same-quote-nested locator string(s)`,
    },
  };
}

function escapeReg(c) {
  return c === "'" ? "\\'" : '\\"';
}
