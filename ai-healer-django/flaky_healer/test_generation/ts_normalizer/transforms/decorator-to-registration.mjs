/**
 * Convert `@Given('text', ...)` / `@When(...)` / `@Then(...)` decorators on
 * class methods into module-level `Given('text', async function(...) { body });`
 * registration calls. Cucumber-JS discovers steps via module-level calls;
 * decorators on classes are never executed.
 *
 * Idempotent: after one pass there are no decorators left; subsequent passes
 * are no-ops.
 */
import { SyntaxKind } from "ts-morph";

export function convertDecoratorsToRegistrations(source) {
  const registrations = [];        // { verb, stepText, params, body }
  let stripped = 0;

  for (const cls of source.getClasses()) {
    for (const method of cls.getMethods()) {
      const decorators = method.getDecorators();
      // Match the first Given/When/Then/And/But decorator on this method.
      let match = null;
      for (const dec of decorators) {
        const call = dec.getCallExpression();
        const name = dec.getName();
        if (["Given", "When", "Then", "And", "But"].includes(name) && call) {
          match = { dec, call, name };
          break;
        }
      }
      if (!match) continue;

      // First argument = step text (string literal, template literal, or regex)
      const args = match.call.getArguments();
      if (args.length === 0) continue;

      const stepText = args[0].getText();
      const params = method.getParameters().map(p => p.getText()).join(", ");
      const body = method.getBody()?.getText() ?? "{}";
      // Strip the outer { } so we can rebuild with `async function` shape.
      const bodyInside = body.startsWith("{") && body.endsWith("}")
        ? body.slice(1, -1)
        : body;

      registrations.push({
        verb:      match.name,
        stepText,
        params,
        body:      bodyInside.trim(),
      });
      stripped += 1;
    }
  }

  if (registrations.length === 0) {
    return { changed: false };
  }

  // Emit each registration as text and append at module scope. We do this
  // BEFORE dropping the class wrapper because the strip-class-wrapper
  // transform will empty the class of these methods afterwards.
  const emitted = registrations.map(r => {
    return `${r.verb}(${r.stepText}, async function (${r.params}) {\n${indentBody(r.body)}\n});`;
  }).join("\n\n");

  // Append at the very end of the file — safe because Cucumber only needs
  // module-scope calls to happen; ordering vs. imports doesn't matter here.
  source.addStatements("\n" + emitted + "\n");

  // Now remove every decorator-annotated method from every class. If the
  // class ends up empty, drop it entirely (strip-class-wrapper handles the
  // `export default class` case; this handles the class if it's still there
  // for other reasons).
  for (const cls of source.getClasses()) {
    const kept = [];
    for (const method of cls.getMethods()) {
      const decorators = method.getDecorators();
      const hasStepDec = decorators.some(d =>
        ["Given", "When", "Then", "And", "But"].includes(d.getName())
      );
      if (hasStepDec) {
        method.remove();
      } else {
        kept.push(method);
      }
    }
  }

  return {
    changed: true,
    report: {
      name: "decorator-to-registration",
      detail: `moved ${stripped} @Given/@When/@Then decorator method(s) to module-level registration calls`,
    },
  };
}

function indentBody(body) {
  return body
    .split("\n")
    .map(line => line.trim() ? "  " + line : line)
    .join("\n");
}
