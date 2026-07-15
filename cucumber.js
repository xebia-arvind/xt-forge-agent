/**
 * Cucumber-JS config — Phase 6.
 *
 * `default` profile runs every feature under features/.
 * `tenant` profile targets a single client's directory when TENANT_SLUG is set,
 * so the Executor can invoke `npx cucumber-js --profile tenant` from Django.
 *
 * Step definitions live in features/steps/**\/*-steps.ts.
 * Shared world / hooks live in features/support/*.ts.
 */

const tenantSlug = (process.env.TENANT_SLUG || "").replace(/[^A-Za-z0-9_-]/g, "");

const common = {
    requireModule: ["ts-node/register"],
    require: [
        "features/support/**/*.ts",
        "features/steps/**/*-steps.ts",
    ],
    format: ["progress", "json:test-results/cucumber-report.json"],
    formatOptions: { snippetInterface: "async-await" },
    publishQuiet: true,
};

module.exports = {
    default: {
        ...common,
        paths: ["features/**/*.feature"],
    },
    tenant: {
        ...common,
        // If TENANT_SLUG is missing we fall back to running everything under
        // features/, which is safe but not scoped. The Executor always sets it.
        paths: tenantSlug ? [`features/${tenantSlug}/**/*.feature`] : ["features/**/*.feature"],
    },
};
