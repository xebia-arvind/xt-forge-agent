#!/usr/bin/env node
/**
 * XT-Forge TypeScript normalizer sidecar.
 *
 * Reads one JSON request from stdin, applies the appropriate transform
 * pipeline for the artifact type, writes one JSON response to stdout.
 *
 * Never throws: on any error the response has `ok: false` and Django falls
 * back to the legacy regex normalizer.
 */
import { readFileSync } from "node:fs";
import { Project, ScriptTarget, ModuleKind, IndentationText } from "ts-morph";

// Pre-parse (string-level)
import { unescapeLiteralNewlines }              from "./transforms/unescape-newlines.mjs";
import { flipInnerSameQuotes }                  from "./transforms/flip-quotes.mjs";
// AST — step-defs
import { convertDecoratorsToRegistrations }     from "./transforms/decorator-to-registration.mjs";
import { stripExportDefaultClassWrapper }       from "./transforms/strip-class-wrapper.mjs";
import { rewritePageObjectImportsToNamed }      from "./transforms/named-imports.mjs";
import { stripModuleScopeInstantiations }       from "./transforms/module-scope-new.mjs";
import { injectInstantiations }                 from "./transforms/inject-instantiation.mjs";
import { injectSlugIntoImports }                from "./transforms/slug-injection.mjs";
import { remapAndBut }                          from "./transforms/and-but-remap.mjs";
import { rebuildCucumberImport }                from "./transforms/cucumber-import.mjs";
import { ensureExpectImport }                   from "./transforms/expect-import.mjs";
import { wrapToBeVisible }                      from "./transforms/wrap-tobevisible.mjs";
// AST — page-object
import { normalizePageObjectExports }           from "./transforms/page-object-exports.mjs";
// AST — shared
import { dedupeImports }                        from "./transforms/dedupe-imports.mjs";

// ---------------------------------------------------------------------------
// Read stdin — Django writes a single JSON blob then closes the pipe.
// ---------------------------------------------------------------------------
function readStdinSync() {
  try {
    return readFileSync(0, "utf8");
  } catch (e) {
    return "";
  }
}

function respond(payload) {
  process.stdout.write(JSON.stringify(payload));
  process.stdout.write("\n");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  const raw = readStdinSync();
  let req;
  try {
    req = JSON.parse(raw);
  } catch (e) {
    respond({ ok: false, errors: [`stdin not JSON: ${e.message}`] });
    process.exit(0);
  }

  const artifactType = String(req.artifact_type || "");
  const relPath = String(req.relative_path || "unknown.ts");
  let content = String(req.content || "");
  const ctx = req.context || {};

  const transformations = [];

  // ---------- Pre-parse rewrites (string-level; safe pre-AST) ------------
  const preRewrites = [
    unescapeLiteralNewlines,
    flipInnerSameQuotes,
  ];
  for (const fn of preRewrites) {
    const out = fn(content);
    if (out.changed) {
      content = out.content;
      transformations.push(out.report);
    }
  }

  // ---------- AST-level rewrites ---------------------------------------
  const project = new Project({
    useInMemoryFileSystem: true,
    compilerOptions: {
      target: ScriptTarget.ES2020,
      module: ModuleKind.CommonJS,
      strict: false,
      noEmit: true,
      allowJs: true,
      experimentalDecorators: true,   // parse decorator syntax we then strip
    },
    manipulationSettings: { indentationText: IndentationText.TwoSpaces },
  });

  let source;
  try {
    source = project.createSourceFile(relPath, content, { overwrite: true });
  } catch (e) {
    respond({
      ok: false,
      content,
      transformations,
      errors: [`ts-morph createSourceFile failed: ${e.message}`],
    });
    process.exit(0);
  }

  try {
    if (artifactType === "STEP_DEFINITIONS") {
      runStepDefsPipeline(source, ctx, transformations);
    } else if (artifactType === "PAGE_OBJECT") {
      runPageObjectPipeline(source, ctx, transformations);
    }
    // dedupe runs last, on any artifact type
    applyTransform(dedupeImports, source, ctx, transformations);

    const outContent = source.getFullText();
    const diagnostics = source.getPreEmitDiagnostics().map(d => ({
      line:    d.getLineNumber?.() ?? 0,
      code:    d.getCode?.() ?? 0,
      message: d.getMessageText()?.toString?.() ??
               String(d.getMessageText?.() ?? ""),
    })).slice(0, 20);

    respond({
      ok: true,
      content: outContent,
      transformations,
      errors: [],
      diagnostics,
    });
  } catch (e) {
    respond({
      ok: false,
      content,
      transformations,
      errors: [`AST transform crashed: ${e.message}\n${e.stack || ""}`],
    });
    process.exit(0);
  }
}

// ---------------------------------------------------------------------------
// Pipelines
// ---------------------------------------------------------------------------
function runStepDefsPipeline(source, ctx, transformations) {
  // Order chosen so each step operates on a well-formed input:
  //   1. Decorators → registrations (before we strip the class shell).
  //   2. Strip class wrapper (now empty of step methods).
  //   3. Named imports (so subsequent import passes see the right shape).
  //   4. Slug injection on page-object imports.
  //   5. Module-scope `new` collection.
  //   6. Inject instantiations inside step bodies using collected names.
  //   7. And/But → previous verb.
  //   8. Cucumber import rebuild (reflects final verb set).
  //   9. Expect import (added last so other transforms can't shadow it).
  applyTransform(convertDecoratorsToRegistrations, source, ctx, transformations);
  applyTransform(stripExportDefaultClassWrapper,   source, ctx, transformations);
  applyTransform(rewritePageObjectImportsToNamed,  source, ctx, transformations);
  applyTransform(injectSlugIntoImports,            source, ctx, transformations);

  // module-scope-new + inject-instantiation coupling — pass the stripped list.
  const strippedReport = stripModuleScopeInstantiations(source);
  if (strippedReport.changed) transformations.push(strippedReport.report);
  const injectReport = injectInstantiations(source, ctx, strippedReport.stripped || []);
  if (injectReport.changed) transformations.push(injectReport.report);

  applyTransform(remapAndBut,             source, ctx, transformations);
  applyTransform(rebuildCucumberImport,   source, ctx, transformations);
  // Wrap bare `locator.toBeVisible()` in `expect(locator).toBeVisible()`
  // BEFORE ensuring the expect import — the wrap introduces expect(...)
  // calls that the next pass must then import.
  applyTransform(wrapToBeVisible,         source, ctx, transformations);
  applyTransform(ensureExpectImport,      source, ctx, transformations);
}

function runPageObjectPipeline(source, ctx, transformations) {
  applyTransform(normalizePageObjectExports, source, ctx, transformations);
  // Page-objects frequently contain assertion helpers (verifyX / expectX)
  // that misuse Locator matchers — same fix as step-defs.
  applyTransform(wrapToBeVisible,            source, ctx, transformations);
  applyTransform(ensureExpectImport,         source, ctx, transformations);
}

function applyTransform(fn, source, ctx, transformations) {
  const report = fn(source, ctx);
  if (report && report.changed && report.report) {
    transformations.push(report.report);
  }
}

main().catch((e) => {
  respond({ ok: false, errors: [`main crashed: ${e.message}`] });
  process.exit(0);
});
