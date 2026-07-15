/**
 * Ensure every top-level page-object class is `export class Foo`, and drop
 * any `export default new Foo()` module-scope side effects.
 *
 * Step-defs import the CLASS and instantiate `new Foo(this.page)` inside
 * step bodies. A default-exported instance breaks that pattern.
 *
 * Idempotent: `export class` remains `export class`; the default instance
 * is only added once (or not at all).
 */
export function normalizePageObjectExports(source) {
  let changed = false;
  const details = [];

  // 1) Named-export every top-level class.
  for (const cls of source.getClasses()) {
    if (cls.isExported()) continue;
    // hasDefaultKeyword should be false — if it's `export default class`,
    // we still want to keep it exported (rename below), not add another.
    if (cls.isDefaultExport()) continue;
    cls.setIsExported(true);
    changed = true;
    details.push(`named-exported class ${cls.getName?.() ?? "(anonymous)"}`);
  }

  // 2) Drop `export default new Foo(...)` module-scope assignments.
  for (const stmt of source.getStatements()) {
    if (stmt.getKindName() !== "ExportAssignment") continue;
    const expr = stmt.getExpression?.();
    if (!expr) continue;
    if (expr.getKindName() !== "NewExpression") continue;
    stmt.remove();
    changed = true;
    details.push("dropped `export default new Foo(...)` module-scope instance");
  }

  // 3) Convert `export default class Foo { … }` → `export class Foo { … }`.
  for (const cls of source.getClasses()) {
    if (!cls.isDefaultExport?.()) continue;
    // ts-morph exposes toggle helpers.
    cls.setIsDefaultExport(false);
    cls.setIsExported(true);
    changed = true;
    details.push(
      `converted default export to named export for class ${cls.getName?.() ?? "(anonymous)"}`,
    );
  }

  if (!changed) return { changed: false };
  return {
    changed: true,
    report: {
      name: "page-object-exports",
      detail: details.join("; "),
    },
  };
}
