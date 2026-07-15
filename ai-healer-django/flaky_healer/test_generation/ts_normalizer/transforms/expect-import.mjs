/**
 * Prepend `import { expect } from '@playwright/test';` if the body calls
 * `expect(` but no `expect` symbol is imported.
 *
 * Idempotent: no re-add on second pass.
 */
export function ensureExpectImport(source) {
  const text = source.getFullText();
  if (!/\bexpect\s*\(/.test(text)) return { changed: false };

  // Look for any existing `expect` import.
  for (const imp of source.getImportDeclarations()) {
    const named = imp.getNamedImports().map(n => n.getName());
    if (named.includes("expect")) return { changed: false };
  }

  source.insertImportDeclaration(0, {
    moduleSpecifier: "@playwright/test",
    namedImports: ["expect"],
  });
  return {
    changed: true,
    report: {
      name: "expect-import",
      detail: "added `import { expect } from '@playwright/test'`",
    },
  };
}
