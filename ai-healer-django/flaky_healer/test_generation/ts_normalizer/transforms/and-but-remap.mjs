/**
 * Rewrite `And('text', ...)` / `But('text', ...)` module-level calls to the
 * previous Given/When/Then verb. `@cucumber/cucumber` v11 does NOT export
 * `And` or `But` — those Gherkin keywords are just aliases and step
 * registrations must be attached to the underlying verb.
 *
 * Order matters: we walk statements top-to-bottom, tracking the last real
 * verb we saw, and remap each And/But to it.
 *
 * Idempotent: no And/But calls left after one pass.
 */
export function remapAndBut(source) {
  let remapped = 0;
  let lastVerb = "Given";       // reasonable default if the file opens with And

  for (const stmt of source.getStatements()) {
    if (stmt.getKindName() !== "ExpressionStatement") continue;

    const call = stmt.getExpression();
    if (!call || call.getKindName() !== "CallExpression") continue;

    const callee = call.getExpression();
    if (!callee) continue;
    const name = callee.getText();

    if (name === "Given" || name === "When" || name === "Then") {
      lastVerb = name;
      continue;
    }
    if (name !== "And" && name !== "But") continue;

    callee.replaceWithText(lastVerb);
    remapped += 1;
  }

  if (remapped === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "and-but-remap",
      detail: `remapped ${remapped} And/But(...) call(s) to their underlying Given/When/Then verb`,
    },
  };
}
