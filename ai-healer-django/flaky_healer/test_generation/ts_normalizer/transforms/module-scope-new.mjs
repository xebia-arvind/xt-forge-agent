/**
 * Delete module-scope `const foo = new FooPage(this.page);` declarations.
 *
 * The LLM keeps writing:
 *   const homePage = new HomePage(this.page);
 *   Given('...', async function () { await homePage.click(); });
 *
 * `this.page` at module scope crashes on file load (`this` is undefined).
 * The pattern belongs INSIDE step bodies.
 *
 * We collect the identifiers we stripped so `inject-instantiation` can
 * re-add them inside each referring step body.
 *
 * Idempotent: nothing at module scope after one pass.
 */
export function stripModuleScopeInstantiations(source) {
  const stripped = [];   // { name, className }

  for (const stmt of source.getStatements()) {
    if (stmt.getKindName() !== "VariableStatement") continue;

    const decls = stmt.getDeclarationList()?.getDeclarations() ?? [];
    if (decls.length !== 1) continue;

    const decl = decls[0];
    const init = decl.getInitializer();
    if (!init || init.getKindName() !== "NewExpression") continue;

    const initText = init.getText();
    // Only strip if the constructor call references `this` (which crashes at
    // module scope). Legit module-scope `new Foo()` calls are left alone.
    if (!/\bthis\b/.test(initText)) continue;

    const varName = decl.getName();
    const className = init.getExpression?.().getText?.() ?? null;

    stripped.push({ name: varName, className });
    stmt.remove();
  }

  if (stripped.length === 0) return { changed: false };
  return {
    changed: true,
    // Stash stripped names on the source so `inject-instantiation` can
    // read them without a second traversal.
    stripped,
    report: {
      name: "module-scope-new",
      detail: `stripped ${stripped.length} module-scope 'new X(this.page)' declaration(s)`,
    },
  };
}
