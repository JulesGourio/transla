# tests

The pytest suite of the backend. The frontend has its own two unit-test files (`client/src/lib/fetchJson.test.ts`,
`client/src/components/translate/reviewPanel/textMatch.test.ts`), run with `bun run test` in [client](../client/README.md).

Parent: [root README](../README.md).

## Run

```bash
uv pip install -e ".[dev]"
python -m pytest                 # from the repository root
python -m pytest tests/test_extract.py -k hyphen
```

No test needs Databricks, a database, a model endpoint or LibreOffice. A test that needs a model call replaces the HTTP
transport; one that needs the database uses the fake below.

## Files

| File | Covers |
|---|---|
| `docx_factory.py` | Builds minimal `.docx` files from raw XML for the tests (body, tables, text boxes, content controls, images) |
| `fakedb.py` | An SQLite stand-in for the asyncpg pool. It runs the router's real SQL for simple statements (`$n` placeholders, `COALESCE`, `CASE`, `RETURNING`, `NOW()`). Postgres-only syntax such as `ANY($1::bigint[])` is out of its reach, so a test that needs it stays at the function level |
| `test_translate.py` | Language detection (French not lost to Spanish, ties, no signal, digit-first references, dominant-language bias), answer parsing, glossary filtering, batch id mapping, glossary candidate extraction, pixel diff, review comments and their injection, fit check and length adaptation guards, inline pre-split, per-segment translation planning |
| `test_extract.py` | Extraction: distinct ids for text boxes in different cells and for nested content controls, VML text boxes, tables in text boxes, non-breaking hyphens, control characters in a translation, part numbers with a non-breaking hyphen |
| `test_router_jobs.py` | The router with the fake database: error messages survive the upload task, previews are served only for finished jobs and revalidated, rebuild clears old previews, retranslating inline segments sends the source side only, file validation (not a zip, no Word part, zip bomb), edits refused while a job runs, blank translations refused, DNT rule validation, a job whose original could not be stored fails early, length anomalies are flagged, unplaced translations are flagged, paging is clamped, the heartbeat |
| `test_translation_batches.py` | Batching: a failed write is retried, a string that can never be stored is reported failed, the second attempt resends only what is missing, the character budget, progress counting |
| `test_llm_retries.py` | `call_llm_json`: rate limits and timeouts are retried, a persistent gateway error gives up with its status, a client error is not retried, an answer cut at `max_tokens` is not sent for repair |
| `test_pages.py` | Page specification parsing and the segment-to-page mapping |
| `test_docx_images.py` | Images are converted to PNG when needed and sent with their own type |
| `test_deploy_config.py` | The bundle, `target_env.json`, `app.yaml` and the deploy script agree; every job follows the job format and points to an existing notebook; the rendered env file is LF; requirements are pinned |
| `test_code_conventions.py` | No `print`, a Sphinx docstring and a return annotation on every function, every parameter documented, no `DBTITLE`, no commented-out code, no narrating comment, route modules under 600 lines |
| `test_lakebase_copy.py` | The pure functions of the Lakebase copy notebook, loaded from the notebook cell itself |

## Rules

- A bug fixed in the pipeline gets a test named after the behaviour it protects, in the file of the module.
- A test builds the smallest document that shows the behaviour; it does not rely on a customer document.
- A test never depends on the order of other tests or on the clock.

## See also

- [services/translation](../server/services/translation/README.md): the modules most tests exercise.
- [routers](../server/routers/README.md): the routes `test_router_jobs.py` calls.
