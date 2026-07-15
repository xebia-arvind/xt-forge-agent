/**
 * For each Given/When/Then/And/But(...) module-level call, look at its async
 * function body. Any identifier that was stripped by `module-scope-new`
 * (i.e. previously module-scope `const foo = new FooPage(this.page);`) that
 * the body references gets a fresh `const foo = new FooPage(this.page);`
 * inserted at the top of the body.
 *
 * Idempotent: on a second pass every referenced var is already declared, so
 * we don't insert again.
 */
export function injectInstantiations(source, ctx, strippedList) {
  if (!strippedList || strippedList.length === 0) return { changed: false };

  const byName = new Map();
  for (const item of strippedList) {
    byName.set(item.name, item.className);
  }

  let injected = 0;

  for (const stmt of source.getStatements()) {
    if (stmt.getKindName() !== "ExpressionStatement") continue;

    const call = stmt.getExpression();
    if (!call || call.getKindName() !== "CallExpression") continue;

    const verb = call.getExpression()?.getText?.();
    if (!["Given", "When", "Then", "And", "But"].includes(verb)) continue;

    const args = call.getArguments();
    if (args.length < 2) continue;
    // Second argument should be the async function.
    const fn = args[1];
    const kind = fn.getKindName();
    if (kind !== "FunctionExpression" && kind !== "ArrowFunction") continue;

    const body = fn.getBody();
    if (!body || body.getKindName() !== "Block") continue;

    const bodyText = body.getText();

    // Find every stripped identifier referenced in the body.
    const toInject = [];
    for (const [name, className] of byName) {
      const referenced = new RegExp(`\\b${escapeReg(name)}\\b`).test(bodyText);
      if (!referenced) continue;
      // Skip if already declared inside the block.
      const alreadyDeclared = new RegExp(
        `\\b(?:const|let|var)\\s+${escapeReg(name)}\\b`,
      ).test(bodyText);
      if (alreadyDeclared) continue;
      toInject.push({ name, className });
    }

    if (toInject.length === 0) continue;

    // Insert declarations as the first statements in the block.
    // ts-morph's Block has insertStatements(idx, textOrArray).
    const decls = toInject.map(t =>
      t.className
        ? `const ${t.name} = new ${t.className}(this.page);`
        : `const ${t.name} = /* unknown class */;`,
    );
    body.insertStatements(0, decls);
    injected += toInject.length;
  }

  if (injected === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "inject-instantiation",
      detail: `inserted ${injected} page-object 'const foo = new Foo(this.page)' declaration(s) into step bodies`,
    },
  };
}

function escapeReg(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
