/**
 * Drop `export default class Steps { … }` wrappers in step-defs files.
 * Cucumber-JS discovers steps via module-level calls, never class methods.
 *
 * By the time this runs, `decorator-to-registration` has already moved every
 * step-registration method out of the class. This transform is what removes
 * the (now-empty or effectively-empty) shell + its constructor.
 *
 * Idempotent: no `export default class` left after one pass.
 */
export function stripExportDefaultClassWrapper(source) {
  let removed = 0;

  for (const cls of source.getClasses()) {
    // Only strip classes that are `export default class`. Named-export
    // classes (e.g. a legit page-object class) should never appear in a
    // step-defs file, but if one does we leave it alone.
    if (!cls.hasDefaultKeyword?.() && !cls.isDefaultExport?.()) continue;

    // If the class still has methods we DIDN'T convert (weird non-step
    // method), keep it — safer than dropping the file's only symbol.
    if (cls.getMethods().length > 0) continue;

    cls.remove();
    removed += 1;
  }

  if (removed === 0) return { changed: false };
  return {
    changed: true,
    report: {
      name: "strip-class-wrapper",
      detail: `removed ${removed} 'export default class' wrapper(s)`,
    },
  };
}
