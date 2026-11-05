# services

The logic behind the routes. The routers call the services; the services never import the routers.

Parent: [server](../README.md). Callers: [routers](../routers/README.md).

## Files

| Path | Content |
|---|---|
| `lakebase.py` | The Lakebase (Postgres) connection and schema |
| `llm.py` | One function to call a serving endpoint and get JSON back, and the cost estimate |
| `soffice.py` | LibreOffice discovery and the `.docx` to PDF conversion |
| `storage.py` | Reading and writing job files, in a volume or on local disk |
| `user.py` | Who is calling, from the request headers |
| [`translation/`](translation/README.md) | The translation engine: extraction, language detection, audit, fit check, rebuild, validation, images, pages, glossary helpers |
| [`processors/`](processors/README.md) | The in-memory adapter between the router and the engine |

### lakebase.py

An `asyncpg` pool (1 to 10 connections, TLS required, UTC) on the Lakebase project named by `LAKEBASE_PROJECT_ID`,
branch `LAKEBASE_BRANCH`, endpoint `LAKEBASE_ENDPOINT`.

- The host comes from the project's endpoints. The password is a database credential generated through the SDK; when that
  call fails, the workspace token is used. The credential expires in about an hour, so a background task builds a new
  pool before the expiry and swaps it in.
- `init_lakebase` connects, creates the database `LAKEBASE_DATABASE` when it does not exist, then creates the tables
  ([schema](../../docs/lakebase_schema.md)). If anything fails, the app starts with no pool and every route that needs
  the database answers `503`.
- `get_pool()` returns the pool or `None`. `store_error` writes an unhandled exception to the `errors` table.

### llm.py

`call_llm_json(host, token, endpoint, messages)` posts one non-streaming chat completion and parses the answer as one
JSON object (a Markdown fence around it is removed). It returns the object and the token usage.

- Rate limits, gateway errors (429, 500, 502, 503, 504), timeouts and dropped connections are retried after 2, 6 and
  15 seconds (a longer `Retry-After` wins, up to 60 seconds). Other client errors are not retried.
- An endpoint that refuses an explicit `temperature` is called again without it, and remembered for the rest of the call.
- A reply that is not JSON is sent back once with a request to fix it, and the token usage of both calls is added up. A
  reply cut off at `max_tokens` is not repaired, since the missing text cannot be recovered: it fails.
- Text that was decoded as Latin-1 instead of UTF-8 is repaired before parsing.
- `cost_eur` multiplies the usage by the per-endpoint prices in the module and by `EUR_PER_USD`.

### soffice.py

Finds a `soffice` binary once per process, in this order: `SOFFICE_PATH`; a tree already extracted in
`SOFFICE_EXTRACT_DIR`; the archive named by `SOFFICE_ARCHIVE_VOLUME_PATH`, downloaded and extracted once (the folder is
named after the archive file, so a new archive name replaces the old tree); then the usual system locations.

`convert_docx_to_pdf` runs LibreOffice headless on a copy of the document, with a profile of its own per call, at most
`SOFFICE_MAX_CONCURRENT` at a time and `SOFFICE_CONVERT_TIMEOUT_S` seconds each. Results are cached on local disk by the
SHA-256 of the input; a cache hit needs no binary. `SofficeUnavailable` means no engine; `SofficeConversionError` that a
conversion failed. `soffice_status()` reports availability without raising.

### storage.py

`job_root()` is `TRANSLATE_VOLUME_PATH`, else `TRANSLATE_LOCAL_STORAGE_DIR`, else a temporary folder. Paths under
`/Volumes/` go through the SDK files API (the app's service principal must hold the grants); any other path is a local
file. `delete` ignores a file that is already gone.

### user.py

`get_user_identity` splits the `x-forwarded-user` header into `user_id`, `workspace_id` and `email`. A header that looks
like an email is used as is; a numeric `<id>@<workspace>` value gives the id and the workspace. Without the header, the
account of the SDK credentials is used when `ENV` is `development` (the default of this module). `require_translate` allows
every request.

## Dependencies

- `translation/` imports `llm.py` (glossary extraction), `soffice.py` (page mapping) and nothing from the routers.
- `processors/translation.py` imports `translation/`.
- Only `routers/translate.py` and `app.py` import `lakebase.py`.

## Rules

- Blocking calls run in a thread through `asyncio.to_thread`.
- Everything that touches a volume goes through `storage.py`.
- Log writes and other best-effort steps log their own failure and never raise into the request.

## See also

- [routers](../routers/README.md): the callers.
- [docs/pipeline.md](../../docs/pipeline.md): the pipeline these services run.
- [docs/lakebase_schema.md](../../docs/lakebase_schema.md): the tables.
