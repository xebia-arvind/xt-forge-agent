/**
 * Rebuild the `import { … } from '@cucumber/cucumber'` line so it exactly
 * matches what the file actually uses. Adds missing verbs, drops stale
 * And/But (already remapped by `and-but-remap`).
 *
 * Also ensures `setDefaultTimeout` / `Before` / `After` etc. remain if the
 * file uses them.
 *
 * Idempotent: on a second pass the set of used verbs is unchanged.
 */
const KNOWN_CUCUMBER_EXPORTS = new Set([
  "Given", "When", "Then",
  "Before", "After", "BeforeAll", "AfterAll",
  "setDefaultTimeout", "setWorldConstructor",
  "DataTable",
  "World", "IWorldOptions",
]);

export function rebuildCucumberImport(source) {
  const bodyText = source.getFullText();

  // Which known Cucumber-JS symbols does the file reference?
  const used = new Set();
  for (const sym of KNOWN_CUCUMBER_EXPORTS) {
    const re = new RegExp(`\\b${escapeReg(sym)}\\b`);
    if (re.test(bodyText)) used.add(sym);
  }

  // Find the existing cucumber import (if any).
  const cucumberImports = source
    .getImportDeclarations()
    .filter(imp => imp.getModuleSpecifierValue() === "@cucumber/cucumber");

  if (used.size === 0) {
    // File doesn't reference any Cucumber symbol — drop stale imports.
    if (cucumberImports.length === 0) return { changed: false };
    for (const imp of cucumberImports) imp.remove();
    return {
      changed: true,
      report: {
        name: "cucumber-import",
        detail: `removed ${cucumberImports.length} unused @cucumber/cucumber import(s)`,
      },
    };
  }

  const wanted = Array.from(used).sort();

  if (cucumberImports.length === 0) {
    // No existing import — add one at the top.
    source.insertImportDeclaration(0, {
      moduleSpecifier: "@cucumber/cucumber",
      namedImports: wanted,
    });
    return {
      changed: true,
      report: {
        name: "cucumber-import",
        detail: `added @cucumber/cucumber import for { ${wanted.join(", ")} }`,
      },
    };
  }

  // Keep the first import, drop the rest, rewrite its named list.
  const primary = cucumberImports[0];
  for (let i = 1; i < cucumberImports.length; i += 1) cucumberImports[i].remove();

  const existing = primary.getNamedImports().map(n => n.getName()).sort();
  if (JSON.stringify(existing) === JSON.stringify(wanted)) {
    return { changed: false };
  }

  primary.removeNamedImports();
  primary.addNamedImports(wanted.map(n => ({ name: n })));

  return {
    changed: true,
    report: {
      name: "cucumber-import",
      detail: `rebuilt @cucumber/cucumber import to { ${wanted.join(", ")} }`,
    },
  };
}

function escapeReg(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
