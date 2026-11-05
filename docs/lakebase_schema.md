# Lakebase schema

The tables of the `latlang` database. `server/services/lakebase.py` creates them at startup (`CREATE TABLE IF NOT EXISTS`,
then `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for later columns). It never drops or truncates a table.

Parent: [docs](README.md). Writers: [routers](../server/routers/README.md). Nightly Delta copy:
[resources](../resources/README.md).

## Jobs

### `translation_jobs`

One row per uploaded document.

| Column | Type | Content |
|---|---|---|
| `id` | serial | Job id, used in every URL |
| `created_at`, `updated_at` | timestamptz | |
| `user_id`, `workspace_id` | text | Owner, from the `x-forwarded-user` header. Every job route filters on `user_id` |
| `status` | text | Position in the state machine ([pipeline](pipeline.md#job-states)) |
| `stage_progress` | text (JSON) | Counters and lists shown by the status panel: `done`/`total` while translating; mode, language pair and counts after the analysis; fit-check, validation and residual-language results after the rebuild |
| `source_lang`, `target_lang` | text | ISO codes picked at upload (`en fr es de cs bg pt ar`) |
| `original_filename` | text | |
| `input_volume_path`, `output_volume_path` | text | `<root>/<job_id>/input_<name>.docx` and `<root>/<job_id>/output.docx`. A null `input_volume_path` means the upload could not be stored |
| `segment_count`, `needs_translation_count` | integer | Segments found; segments that are neither do-not-translate, numeric-only nor outside the page range |
| `error_type`, `error_msg` | text | Set together with `status = 'failed'` (or `ValidationFailed` with the failed checks as JSON) |
| `worker_pid`, `worker_heartbeat`, `claimed_at` | | The process driving the job and its last sign of life |
| `llm_call_count`, `total_input_tokens`, `total_output_tokens`, `total_cost_eur` | | Running totals of the model calls |
| `notes` | text | Free-text reviewer notes |
| `glossary_validated_at` | timestamptz | Set when the reviewer signs the translation off; gates the glossary proposals |
| `share_token` | text, unique | Set the first time the job is shared; never rotated |
| `selected_image_paths` | text (JSON array) | `word/media/...` files chosen for image OCR |
| `page_filter` | text | The page specification typed at upload, as typed |

Index `translation_jobs_user_idx` on `user_id`; unique partial index on `share_token`.

### `translation_segments`

One row per extracted paragraph (and per OCR'd image). Unique on `(job_id, seg_id)`; deleted with the job.

| Column | Content |
|---|---|
| `seg_id` | Position-based id such as `word/document.xml#table.bt5.r5.c0#p0`. It contains `/`, so the routes take it as a `:path` |
| `part`, `location_type` | The XML part (`word/document.xml`, a header, a footer, ...) and the kind of place: `body_direct`, `table`, `txbx` (text box), `sdt` (content control) or `docx_image_ocr` |
| `xml_choice_path`, `xml_fallback_path` | JSON positional paths `[[tag, child_index], ...]` to the paragraph. A text box exists twice in the XML (`mc:Choice` and `mc:Fallback`), hence two paths. For an image segment `xml_choice_path` holds `{"media_filename": ..., "thumbnail_base64": ...}` |
| `source_text` | Text of the paragraph |
| `detected_lang`, `lang_confidence` | Language label (`??` when no signal) and its confidence; confidence is null once a human or the translation stage has set the language |
| `pattern_type` | `mono`, `dnt`, `numeric_only`, `bilingual_txbx_pair`, `paired_body`, `paired_row`, `bilingual_inline_slash`, `bilingual_inline_concat`, `bilingual_inline_charsplit` |
| `pair_id` | Shared by the two sides of a pair |
| `conflict_flag`, `conflict_detail` | A reason a reviewer should look at the segment, and its text |
| `dnt_tokens` | JSON list of tokens that must survive translation |
| `inline_split` | JSON describing where the two languages sit inside one segment (`kind` is `slash`, `char` or `format`) |
| `translated_text` | The stored translation. For a `format` split it is the translated span only |
| `keep_as_is` | The segment is rebuilt with its source text |
| `answered_question_id` | |
| `warning_dismissed` | The reviewer accepted the "still reads as source language" warning |
| `out_of_page_range` | The segment lies outside the requested pages |

### `translation_questions`

The clarifying questions of a bilingual document. Unique on `(job_id, q_id)`.

| Column | Content |
|---|---|
| `q_id`, `seg_ids` (JSON), `category` | Question id (`q_001`, ...), the segments it is about, its kind (only `unknown_lang` is generated) |
| `question_text`, `context`, `suggested_answer` | |
| `answer`, `answered_at` | Null until answered |

### `translation_llm_calls`

One row per model call of a job (`endpoint_name`, `batch_size`, `input_tokens`, `output_tokens`, `cost_eur`), including
the image OCR calls. Deleted with the job.

### `translation_feedbacks`

A thumbs-up or thumbs-down on a job: `job_id` (set to null if the job is deleted), `user_id`, `workspace_id`,
`workspace_url`, `vote` (`up` or `down`), `comment`, `resolved`, `resolution_reason`.

## Glossary

### `glossary_terms`

One row per concept, one column per language.

| Column | Content |
|---|---|
| `term_id` | `T0001`, `T0002`, ... allocated under an advisory lock |
| `en fr cs bg de es pt ar` | The term in each language; empty when unknown |
| `domain`, `notes` | |
| `definition`, `definition_source` | The definition and where it came from: `REF#chunk` (a terminology section of a document), `generated` (written by a model) or `cross_lang_pair` |

### `glossary_candidates`

Terms waiting for review. Same language columns as `glossary_terms`, plus `n_docs` (documents that showed the pair),
`sources`, `definition`, `definition_source`, `priority`, `status` (`pending`, `approved`, `rejected`), `reviewed_by`,
`reviewed_at`, `reject_reason`. Index on `(status, priority, n_docs DESC)`.

### `dnt_rules`

The do-not-translate rules a reviewer wrote: `pattern`, `type`, `match_mode` (`exact`, `prefix`, `glob`, `regex`),
`notes`.

## Other

### `errors`

Unhandled exceptions of the API: `user_id`, `workspace_id`, `endpoint`, `error_type`, `error_msg`, `stack_trace`.

## See also

- [pipeline](pipeline.md): how the jobs and segments move through these tables.
- [services](../server/services/README.md): `lakebase.py`, which owns the connection.
