/**
 * Rewrite bare `page.locator(...).toBeVisible()` (and similar `.toHaveText(...)`,
 * `.toHaveCount(...)`, `.toContainText(...)`, `.toBeEnabled()`, `.toBeChecked()`,
 * `.toBeHidden()`, `.toHaveValue(...)`) into the proper form:
 *
 *   BEFORE:  await this.page.locator('#x').first().toBeVisible();
 *   AFTER:   await expect(this.page.locator('#x').first()).toBeVisible();
 *
 * `Locator` doesn't own these matchers — they only exist on the return of
 * `expect(locator)` (see @playwright/test). The LLM confuses `.click()` /
 * `.fill()` (which ARE on Locator) with the matcher methods (which are
 * NOT). This ships as a runtime TypeError otherwise:
 *
 *   TypeError: locator(...).toBeVisible is not a function
 *
 * Idempotent — already-wrapped calls (inside an existing `expect(...)`)
 * are left alone.
 */
import { SyntaxKind } from "ts-morph";

const MATCHER_METHODS = new Set([
  "toBeVisible", "toBeHidden", "toBeEnabled", "toBeDisabled",
  "toBeChecked", "toBeAttached", "toBeEmpty", "toBeFocused",
  "toBeInViewport", "toHaveText", "toContainText",
  "toHaveValue", "toHaveCount", "toHaveClass", "toHaveAttribute",
  "toHaveId", "toHaveCSS", "toHaveURL", "toHaveTitle",
]);

/**
 * Collect (start, end, receiverText) tuples for every bare-matcher call,
 * then apply the rewrites to the raw source text and replace the file
 * body in one shot. This avoids ts-morph's "node forgotten" errors that
 * fire when you mutate `PropertyAccessExpression` receivers mid-walk in
 * a chained call like `locator('x').first().toBeVisible()`.
 *
 * Idempotent: already-wrapped receivers (`expect(...)`) are skipped.
 */
export function wrapToBeVisible(source) {
  const rewrites = [];
  const seenSpans = new Set();

  const callExprs = source.getDescendantsOfKind(SyntaxKind.CallExpression);
  for (const call of callExprs) {
    let propAccess;
    try {
      propAccess = call.getExpression();
    } catch {
      continue;                              // node was invalidated — skip
    }
    if (!propAccess || propAccess.getKindName() !== "PropertyAccessExpression") {
      continue;
    }
    let method;
    try {
      method = propAccess.getName();
    } catch {
      continue;
    }
    if (!MATCHER_METHODS.has(method)) continue;

    let receiver;
    try {
      receiver = propAccess.getExpression();
    } catch {
      continue;
    }
    if (!receiver) continue;

    let receiverText;
    try {
      receiverText = receiver.getText();
    } catch {
      continue;
    }

    // If already inside an `expect(...)`, leave alone (idempotency).
    if (/^expect\s*\(/.test(receiverText.trim())) continue;

    // Only wrap when the receiver clearly resolves to a Locator chain.
    const looksLikeLocator =
      /\.(locator|getBy[A-Z]\w*|first|last|nth|filter)\s*\(?/.test(receiverText) ||
      /\bthis\.page\b/.test(receiverText);
    if (!looksLikeLocator) continue;

    let start, end;
    try {
      start = receiver.getStart();
      end = receiver.getEnd();
    } catch {
      continue;
    }
    const key = `${start}:${end}`;
    if (seenSpans.has(key)) continue;
    seenSpans.add(key);
    rewrites.push({ start, end, receiverText });
  }

  if (rewrites.length === 0) return { changed: false };

  // Apply rewrites right-to-left so earlier offsets stay valid.
  rewrites.sort((a, b) => b.start - a.start);
  let text = source.getFullText();
  for (const r of rewrites) {
    text = text.slice(0, r.start) + `expect(${r.receiverText})` + text.slice(r.end);
  }
  source.replaceWithText(text);

  return {
    changed: true,
    report: {
      name: "wrap-tobevisible",
      detail: `wrapped ${rewrites.length} bare .toBeVisible/.toHaveText/etc. call(s) in expect(...)`,
    },
  };
}
