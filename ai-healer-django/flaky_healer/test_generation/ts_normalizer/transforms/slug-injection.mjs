/**
 * Rewrite page-object import specifiers so they contain the tenant slug and
 * use the correct relative depth.
 *
 * The materialized layout is:
 *   tests/pages/generated/<slug>/HomePage.ts
 *   features/steps/<slug>/steps.ts       ← from here, 3 hops up to reach tests/
 *
 * The LLM tends to emit:
 *   '../../tests/pages/generated/HomePage'        (missing slug, wrong depth)
 * We want:
 *   '../../../tests/pages/generated/<slug>/HomePage'
 *
 * Idempotent: if the slug segment is already present and depth is right,
 * do nothing.
 */
export function injectSlugIntoImports(source, ctx) {
  const slug = (ctx.slug || "").trim();
  if (!slug) return { changed: false };

  let rewritten = 0;

  for (const imp of source.getImportDeclarations()) {
    const spec = imp.getModuleSpecifierValue();
    if (!spec) continue;

    // Only touch relative imports pointing at generated page objects.
    if (!spec.startsWith("../") && !spec.startsWith("./")) continue;
    const marker = "tests/pages/generated";
    const idx = spec.indexOf(marker);
    if (idx === -1) continue;

    const after = spec.slice(idx + marker.length);   // starts with `/…` or empty
    const filename = after.replace(/^\/+/, "");      // strip leading slash(es)
    if (!filename) continue;

    // If filename already starts with `<slug>/…`, only fix depth if needed.
    const parts = filename.split("/");
    const hasSlug = parts[0] === slug;
    const rebuiltTail = hasSlug ? filename : `${slug}/${filename}`;

    // From features/steps/<slug>/ we need 3 `../` to reach the repo root
    // (../ → <slug>/, ../../ → steps/, ../../../ → features/root).
    const rebuilt = `../../../tests/pages/generated/${rebuiltTail}`;

    if (spec === rebuilt) continue;
    imp.setModuleSpecifier(rebuilt);
    rewritten += 1;
  }

  if (rewritten === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "slug-injection",
      detail: `rewrote ${rewritten} page-object import path(s) to include slug '${slug}' + correct depth`,
    },
  };
}
