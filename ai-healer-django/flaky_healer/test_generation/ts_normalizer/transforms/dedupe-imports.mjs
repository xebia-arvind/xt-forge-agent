/**
 * Collapse duplicate imports from the same module. The LLM sometimes emits
 * two lines like:
 *   import { Given } from '@cucumber/cucumber';
 *   import { When, Then } from '@cucumber/cucumber';
 * →
 *   import { Given, When, Then } from '@cucumber/cucumber';
 *
 * Idempotent: no duplicate specifiers after one pass.
 */
export function dedupeImports(source) {
  const bySpec = new Map();      // module-specifier → [ImportDeclaration…]

  for (const imp of source.getImportDeclarations()) {
    const key = imp.getModuleSpecifierValue();
    if (!bySpec.has(key)) bySpec.set(key, []);
    bySpec.get(key).push(imp);
  }

  let collapsed = 0;

  for (const [_spec, imps] of bySpec) {
    if (imps.length < 2) continue;

    // Merge into the first import declaration; delete the rest.
    const target = imps[0];
    const seenNamed = new Set(target.getNamedImports().map(n => n.getName()));
    const targetHasDefault = !!target.getDefaultImport();
    let targetDefault = target.getDefaultImport()?.getText() ?? null;

    for (let i = 1; i < imps.length; i += 1) {
      const other = imps[i];
      // Merge default import
      const otherDefault = other.getDefaultImport()?.getText();
      if (otherDefault && !targetHasDefault) {
        target.setDefaultImport(otherDefault);
        targetDefault = otherDefault;
      }
      // Merge named imports
      for (const n of other.getNamedImports()) {
        const nm = n.getName();
        if (!seenNamed.has(nm)) {
          target.addNamedImport(nm);
          seenNamed.add(nm);
        }
      }
      other.remove();
      collapsed += 1;
    }
  }

  if (collapsed === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "dedupe-imports",
      detail: `merged ${collapsed} duplicate import declaration(s)`,
    },
  };
}
