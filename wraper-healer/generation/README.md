# File-Driven Test Generation

## Purpose
Define new feature scenario outlines in one file, run generator API call, review in Django admin, then approve/materialize.

## Request File
Edit:
- `tests/generation/feature_requests.json`

You can add one or many jobs under `jobs`.

## Run
From repo root:

```bash
npm run gen:testcases
```

Optional custom file:

```bash
node tests/utils/runGenerationFromFile.mjs --file tests/generation/feature_requests.json
```

## What happens
1. Script reads request file.
2. Calls `POST /test-generation/jobs/`.
3. Prints job IDs + detail API links.
4. You manually verify in Django admin:
   - `/admin/test_generation/generationjob/`
5. Approve and materialize from admin quick actions.
6. Generated files are written to:
   - `tests/generated/`
   - `tests/pages/generated/`
