/**
 * `import HomePage from '../../tests/pages/generated/HomePage'`
 *   →
 * `import { HomePage } from '../../tests/pages/generated/HomePage'`
 *
 * Page-object files use `export class HomePage`, so the default import
 * resolves to `undefined` at runtime. This transform detects imports that
 * point at a known page-object path and rewrites them to named form.
 *
 * Idempotent: named imports are left untouched.
 */
const PAGE_OBJECT_PATH_HINTS = [
  "/pages/",
  "/page-objects/",
  "Page",       // last-name-segment heuristic
];

export function rewritePageObjectImportsToNamed(source, ctx) {
  const known = new Set((ctx.known_page_objects || []).map(String));
  let rewritten = 0;

  for (const imp of source.getImportDeclarations()) {
    const spec = imp.getModuleSpecifierValue();
    const defaultImport = imp.getDefaultImport();
    if (!defaultImport) continue;

    const name = defaultImport.getText();

    // Only rewrite if the specifier looks like a page-object path AND the
    // identifier is capitalized like a class. Skip framework defaults
    // (`import test from '@playwright/test'` — but that already uses named
    // form in practice, and we still gate on the path hint).
    const looksLikePagePath = PAGE_OBJECT_PATH_HINTS.some(h => spec.includes(h));
    const looksLikeClassName = /^[A-Z][A-Za-z0-9]*$/.test(name);
    const inKnownList = known.has(name);
    if (!inKnownList && !(looksLikePagePath && looksLikeClassName)) continue;

    imp.removeDefaultImport();
    // Add as a named import — merge with any existing named imports.
    const existingNamed = imp.getNamedImports().map(n => n.getName());
    if (!existingNamed.includes(name)) {
      imp.addNamedImport(name);
    }
    rewritten += 1;
  }

  if (rewritten === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "named-imports",
      detail: `rewrote ${rewritten} default page-object import(s) to named form`,
    },
  };
}
