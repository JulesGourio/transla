# routers

The HTTP routes, all mounted under `/api`.

Parent: [server](../README.md).

## Files

| Module | Content |
|---|---|
| `translate.py` | The translation feature: jobs, segments, previews, sharing, feedback, glossary. Also the orchestration of the pipeline stages (see [docs/pipeline.md](../../docs/pipeline.md)) |
| `config.py` | `GET /config/app` (name, logo) and `GET /me` (user, workspace URL) |
| `health.py` | `GET /health`: `{"status": "healthy", "timestamp": ...}` |

## Routes of `translate.py`

Every route depends on `require_translate`, which allows everyone: access is decided by the permissions of the Databricks
App. A job route answers `404` for a job that belongs to another user.

### Jobs

| Route | What it does |
|---|---|
| `POST /translate/jobs` | Form: `file`, `source_lang`, `target_lang`, optional `selected_images` (JSON array), optional `pages`. Validates, inserts the job, starts the analysis, returns `{id}`. `400` with a readable message for an invalid file or specification |
| `GET /translate/jobs` | The caller's jobs, newest first (`limit` 1 to 200, `offset`) |
| `GET /translate/jobs/{id}` | Status, `stage_progress`, language pair, counts, error, token and cost totals, notes, the questions while they matter, and `stale` when an active job has had no heartbeat for 120 seconds |
| `POST /translate/jobs/{id}/answer` | Stores answers to the questions; `409` unless `awaiting_answers`. The job becomes `answered` when none is left |
| `POST /translate/jobs/{id}/translate` | Starts the translation; `409` unless `answered`, or `failed` after a finished analysis |
| `POST /translate/jobs/{id}/rebuild` | Starts fit check, rebuild and validation; `?include_review_comments=true` adds Word comments. `409` unless `translated`, `failed`, `done` or `done_with_warnings`, or while a planned segment has no translation |
| `POST /translate/jobs/{id}/restart` | Runs the whole pipeline again on the stored original; wipes segments, questions and manual edits. Body `{selected_images}` replaces the image selection, absent keeps it. `409` while the job is active or when the original is not stored |
| `PATCH /translate/jobs/{id}/notes` | Saves the reviewer's notes |
| `POST /translate/jobs/{id}/validate` | Signs the translation off (`done` or `done_with_warnings` only) and starts the glossary proposals |
| `POST /translate/jobs/{id}/share` | Returns the job's share token, creating it the first time |
| `GET /translate/shared/{token}` | Read-only view of a shared job: segments, feedback, and the translated `.docx` in base64. Any user of the app with the link can open it |
| `POST /translate/feedback` | Stores a thumbs up or down (`vote` is `up` or `down`) with an optional `comment` and `job_id` |

### Segments

| Route | What it does |
|---|---|
| `GET /translate/jobs/{id}/segments` | Every segment with its category, flag and detail; `category` filters (`flagged` selects by flag), `limit`/`offset` page the result. Also returns the count of each category |
| `PATCH /translate/jobs/{id}/segments/{seg_id}` | Sets the translation by hand (not empty; `409` while the job is active). The segment becomes human-confirmed |
| `POST .../segments/{seg_id}/retranslate` | Sends that one segment through the model again |
| `PATCH .../segments/{seg_id}/language` | Sets the detected language |
| `POST .../segments/{seg_id}/dismiss-warning` | Excludes the segment from the "still reads as the source language" warnings |

The `seg_id` contains `/`, so these routes use `{seg_id:path}`. The three suffixed routes are declared before the bare one,
which would otherwise match their whole path.

A segment's category is derived from its state: `image_translation`, `kept_page_filtered`, `kept_dnt`, `kept_numeric`,
`translated`, `needs_review` (kept after a failed translation), `kept_other_language`, `pending`.

### Images and previews

| Route | What it does |
|---|---|
| `POST /translate/analyze-images` | Lists the raster images of an uploaded `.docx` with thumbnails; creates nothing |
| `GET /translate/jobs/{id}/images` | The same list for a stored job, plus `previously_selected` |
| `GET /translate/jobs/{id}/preview` | `?format=docx` (default) returns both documents in base64; `?format=pdf` returns both PDFs. `409` until the job is `done` or `done_with_warnings` |
| `GET /translate/jobs/{id}/preview.pdf?side=before` or `side=after` | One PDF as raw bytes, for an `iframe` or pdf.js. `ETag` and `no-cache` |
| `GET /translate/jobs/{id}/preview/diff.png?page=N` | Pixel difference of a page |
| `GET /translate/jobs/{id}/preview/changes` | Bounding boxes of the pixel differences |
| `GET /translate/render-engine` | Whether LibreOffice is usable; the first call may extract the archive (about a minute) |

`501` means LibreOffice is unavailable, `502` that a conversion failed.

### Glossary

| Route | What it does |
|---|---|
| `GET /translate/glossary` | All terms and do-not-translate rules |
| `POST /translate/glossary/terms` | Creates or updates a term (`term_id` empty creates `T####`) |
| `DELETE /translate/glossary/terms/{term_id}` | |
| `POST /translate/glossary/dnt` | Adds a rule; `422` for a rule that matches everything or can never match |
| `DELETE /translate/glossary/dnt/{rule_id}` | |
| `GET /translate/glossary/candidates` | Candidates by `status` (default `pending`), best first |
| `POST /translate/glossary/candidates/{id}/approve` | Turns a candidate into a term, with optional per-language corrections; `409` if already reviewed |
| `POST /translate/glossary/candidates/approve-all` | Approves every pending candidate as is |
| `POST /translate/glossary/candidates/{id}/reject` | Rejects with a reason |

## Rules

- Every query filters on the caller's `user_id`, except the shared view, the glossary and the feedback insert.
- A stage is started with `_spawn`, which keeps a reference to the task so it is not garbage-collected.
- Edits (segment text, language, retranslate) and restart are refused while the job is active (409): the running stage
  would overwrite them.
- A segment edit never changes `output.docx`. The next rebuild does.
- The routes answer in English, with an `error` key; the client shows the text.

## See also

- [docs/pipeline.md](../../docs/pipeline.md): what the stages do.
- [services](../services/README.md): the modules the routes call.
- [docs/lakebase_schema.md](../../docs/lakebase_schema.md): the tables they read and write.
