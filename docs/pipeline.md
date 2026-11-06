# Translation pipeline

How a `.docx` goes from upload to a translated, validated document. The code is in
[routers/translate/](../server/routers/translate/__init__.py) (the routes), the
[pipeline modules](../server/services/README.md#pipeline-modules-of-translation) (the orchestration) and the
[translation engine](../server/services/translation/README.md) (the document work).

Parent: [docs](README.md).

## Job states

```
uploaded -> extracting -> auditing -> awaiting_answers -> answered
  -> translating -> translated -> fit_checking -> rebuilding -> validating
  -> done | done_with_warnings | failed
```

| Status | Meaning | Left by |
|---|---|---|
| `uploaded` | Row created, background task starting | the analysis task |
| `extracting`, `auditing` | The analysis runs | the analysis task |
| `awaiting_answers` | The analysis is stored; the reviewer may answer questions | `POST .../answer` (all answered), or `POST .../translate` when there is nothing to answer |
| `answered` | Every question has an answer | `POST .../translate` |
| `translating` | Model calls running | the translation task |
| `translated` | Every planned segment has a translation or a flag | `POST .../rebuild` |
| `fit_checking`, `rebuilding`, `validating` | Rebuild stage | the rebuild task |
| `done` | Structural checks pass, nothing left to look at | `POST .../rebuild` (re-run), `POST .../restart` |
| `done_with_warnings` | Structural checks pass, but a segment is flagged or still reads as the source language | same as `done` |
| `failed` | An exception, a failed structural check, or an orphaned job | `POST .../translate` (if the analysis finished), `POST .../rebuild`, `POST .../restart` |

The states `uploaded`, `extracting`, `auditing`, `translating`, `fit_checking`, `rebuilding` and `validating` are the
active ones: a worker is driving the job. Restart, retranslate and segment edits are refused while a job is active (409).

## 1. Upload

`POST /api/translate/jobs` takes the file, the language pair, an optional `selected_images` JSON array and an optional
`pages` specification. It refuses, with a message the user can act on, a file that is empty, larger than
`MAX_TRANSLATE_FILE_MB` (60), not named `.docx`, not a zip (corrupted, password-protected, or an old `.doc` renamed), without
`word/document.xml`, or larger than `MAX_TRANSLATE_UNZIPPED_MB` (800) once unzipped. A `pages` value that does not parse
is refused too. It then inserts the job and starts the analysis in the background.

The original is copied to storage (`<root>/<job_id>/input_<name>.docx`) in parallel. The analysis fails at once when that
copy could not be written, because every rebuild and preview starts from it.

## 2. Extract

`extract_docx_segments` reads every text part of the zip (the body, headers, footers, footnotes, endnotes and comments) and
emits one segment per translatable paragraph, from:

- body paragraphs;
- tables, with row and cell coordinates;
- text boxes, drawing and VML, including tables inside a text box. A drawing text box is stored twice in the XML
  (`mc:Choice` and `mc:Fallback`); both positional paths are recorded and a translation is written to both copies;
- structured document tags (content controls), nested ones and those in table cells included.

Each segment carries a position-based `seg_id`, the positional XML path, a formatting signature of its runs, and the
text. Non-breaking hyphens are part of the text.

When the user selected images, the OCR step runs here (see [Image text](#image-text)).

## 3. Audit

`audit_segments` classifies every segment and decides the document mode.

1. **Do-not-translate and numeric-only.** A segment made of part numbers, standards, internal references or dates (the
   built-in `DNT_REGEX`, plus the reviewer's `dnt_rules`) is `dnt`; one made of numbers is `numeric_only`. Neither is ever
   sent to the model.
2. **Language.** Detection is constrained to the declared pair plus the languages the document really holds: a free pass
   discovers them, then each segment is detected again within that set by two detectors (fastText `lid.176` renormalised
   over the set, and a stopword/diacritic heuristic). Only agreement of both reports a confidence of 0.85 or more; one
   detector alone is capped at 0.80. A segment with no signal is labelled `??` and takes the language that dominates the
   document.
3. **Mode.** The document is `bilingual` when confident segments in a language other than the source number at least 5 and
   at least 15 % of the confident segments; otherwise `monolingual`.
4. **Pairing** (bilingual mode only): two paragraphs in one text box, two consecutive body paragraphs, or two table rows,
   in different languages, are paired (`pair_id`, primary and secondary side). A pair is then checked for numeric,
   reference-code and length mismatches, which set `conflict_flag`.
5. **Inline splits** (both modes): one segment that holds two languages is split structurally before any detection:
   `X / Y` (`bilingual_inline_slash`), two formatting groups (`bilingual_inline_concat`, kind `format`), or a script
   switch or sentence boundary (`bilingual_inline_charsplit`). Each side is then labelled on its own.
6. **Questions.** In bilingual mode, up to five segments whose language stays below 0.55 confidence become
   `unknown_lang` questions. A monolingual document asks nothing.

Questions are generated by rules; no model is involved.

## 4. Page filter

With a `pages` value, `assign_pages` converts the original to PDF with LibreOffice and walks the text of each page in
document order, matching each segment forward from the last match to give it a page number. A segment outside the
requested pages gets `out_of_page_range`; one that could not be placed stays translatable. Headers, footers, footnotes,
endnotes and comments are not tied to a page of the body and are always translated, and so are image segments.

`parse_page_spec` accepts `3-10`, `1,4,9` or a mix such as `1-3,7,12-15`. It refuses reversed or absurdly large ranges.

## 5. Answer

`POST .../answer` stores the reviewer's answers. When none is left, the job is `answered`. Before translating, an
`unknown_lang` answer that names exactly one supported language (`cs`, `czech`, `tchèque`, ...) sets the language of the
segments it was about, with confidence 1.0.

## 6. Translate

`POST .../translate` is accepted from `answered`, and from `failed` once the analysis has finished.

1. The mode is recomputed from the stored detections.
2. `_plan_segment_translation` decides, for each segment still without a translation, what goes to the model:
   - `dnt`, `numeric_only` and out-of-range segments: nothing.
   - Inline splits: only the source-language side. The other side survives verbatim. The stored text is the composed
     replacement, or just the translated span for a `format` split.
   - Monolingual mode: everything, except a passage detected with at least 0.85 confidence as a language other than the
     source and at least 5 words long.
   - Bilingual mode: the segments labelled with the source language.
3. Everything not planned is set to `keep_as_is`.
4. The planned texts are deduplicated, in first-occurrence order, and cut into batches of at most 40 strings and
   `TRANSLATE_BATCH_MAX_CHARS` (6000) characters; a longer string travels alone.
5. Each batch is one `call_llm_json` call: the system prompt names the pair, carries the matching glossary terms and the
   translations already obtained in this document (for consistency), and the user message is a JSON list of numbered
   items. Concurrency is bounded per process (`TRANSLATE_MAX_CONCURRENT`) and per job (`TRANSLATE_MAX_CONCURRENT_PER_JOB`).
6. A batch that fails or comes back incomplete is retried once for the missing strings, then string by string (two
   attempts each). A string that still fails is kept, flagged `conflict_flag` with "Translation failed after all
   retries".
7. Each batch is written to the database as soon as it returns, so a restart resumes where it stopped: segments that
   already have a translation are skipped.
8. For every stored translation: tokens listed in `dnt_tokens` that disappeared set the flag "DNT token(s) lost in
   translation"; a translation under 30 % of a source of 60 characters or more ("looks incomplete"), or over three times a
   source of 40 characters or more ("looks too long"), is flagged too.
9. Two segments with the same source text but different translations are flagged "Same source text translated
   differently elsewhere in this document".

Reviewers can also force one segment through the model again (`.../retranslate`), set its language (`.../language`),
type its translation (`PATCH .../segments/{seg_id}`) or accept a warning (`.../dismiss-warning`). Edits do not touch the
output document until the next rebuild.

## 7. Rebuild

`POST .../rebuild` is accepted from `translated`, `failed`, `done` and `done_with_warnings`, and refused when a planned
segment still lacks a translation. The status is claimed atomically, so a double click starts one run.

Each lap of the stage does the following:

1. Download the original, extract it again, and merge each stored segment with its position. A planned segment with no
   translation keeps its source text and is flagged.
2. **Fit check** (`check_fit`): translations likely to overflow their container are flagged. Any growth in a header or
   footer is `CRITICAL`; text boxes tolerate 30 % growth, table cells 50 %, body paragraphs 200 %.
3. **Length adaptation** (`adapt_lengths`): a flagged translation is shortened with the abbreviation rules of the target
   language when it has some (French, English, Spanish, German, Czech; for French, `Monter` -> `Mt.`, `selon` -> `cf.`,
   ...). Other target languages are left as they are.
4. **Rebuild** (`rebuild_docx_bytes`): lxml rewrites the runs of each paragraph at its positional path, keeps namespace
   declarations byte for byte, and copies every other zip entry unchanged. Segments the rebuild could not place are
   flagged.
5. **Review comments** (optional, `include_review_comments`): real Word comments are anchored on flagged segments.
6. **Validate** (`validate_docx`): the structural checks and the residual-language check below.
7. If the document still reads as the source language in places, those segments (minus dismissed, do-not-translate,
   numeric-only and out-of-range ones) are translated again once with a prompt that says why, and the lap repeats. This
   happens at most once per run.

The result is stored as `output.docx`. Flags describing the previous rebuild (still reads as source language, may not
fit, missing, could not be placed) are cleared and written again from this run, so the Segments panel and the status
panel agree.

Validation:

| Check | Kind | Verifies |
|---|---|---|
| `zip_integrity` | structural | every zip entry reads |
| `xml_validity` | structural | every `.xml` and `.rels` part parses |
| `namespace_integrity` | structural | every prefix named in `mc:Ignorable` is declared |
| `auto_prefix` | structural | no `ns0:`, `ns1:` prefixes appeared |
| `content_types` | structural | every part named in `[Content_Types].xml` exists |
| `segment_fidelity` | structural | the rebuilt document has the same segment ids as the original (the comments part is excluded) |
| `byte_identity` | structural | parts that must not change (styles, settings, ...) are identical |
| `residual_source_language` | quality | no segment still reads as the source language |

A failed structural check makes the job `failed` with `ValidationFailed` and the failed checks as the error. A residual
or a flagged segment makes it `done_with_warnings`. Otherwise it is `done`.

After a successful rebuild the two preview PDFs are generated in the background (see [Preview](#preview)).

## Image text

The user may choose images whose text should be translated. `POST /api/translate/analyze-images` lists the raster images of
a `.docx` (`.png .jpg .jpeg .bmp .gif .tiff`, each with a thumbnail and the number of times its content appears) without
creating a job. Identical images are grouped by content hash, so a logo repeated twenty times costs one call.

At analysis time the chosen images go to the vision model (`TRANSLATE_ENDPOINT`); other formats are converted to PNG first.
The step is best effort: a missing endpoint or a failing call skips the images and never fails the job. Each transcription
becomes a segment with `location_type = docx_image_ocr`, `body_p_idx = -1`, and the media file name in
`xml_choice_path`. It is placed in the list next to the image's position in the document, goes through the same
translation, and appears in the Segments panel under "Images" with the thumbnail.

The pixels of an image and the document body are never modified: image translations are shown in the application only.

## Preview

At the end of a successful rebuild, the original and the output are converted to PDF with LibreOffice and written next to
`output.docx` (`preview_original.pdf`, `preview_translated.pdf`). Opening the preview is then two file downloads. An
older job, or one whose PDFs are missing, is converted on first open and the PDFs are saved for the next time. A rebuild
or restart deletes the PDFs first, and every PDF response carries an `ETag` and `Cache-Control: private, no-cache`, so a
stale page is never shown.

`GET /api/translate/render-engine` tells whether LibreOffice is usable on the deployment.

## Background work

There is no task queue. Each stage runs as an `asyncio` task started from the request that triggers it, and a set holds a
reference to it so it is not collected. A process-wide semaphore (`TRANSLATE_MAX_CONCURRENT_JOBS`, default 5) limits the
stages running at once; a job waiting for a reviewer holds no slot.

A running stage refreshes `worker_heartbeat` every 30 seconds. The status of a job in an active state whose heartbeat is
older than 120 seconds is reported with `stale: true`. At startup, any job in an active state whose heartbeat is missing
or older than 2 minutes is set to `failed` with `OrphanedJob`. The states that wait for a human (`awaiting_answers`,
`answered`, `translated`) survive a restart.

## Glossary

The model receives, for each batch, the glossary terms whose source-language form appears in the batch (at most 300 lines)
with their target-language form. Do-not-translate rules from `dnt_rules` take part in the audit through `DntMatcher`.

Terms enter the glossary in three ways:

- by hand, in the Glossary panel;
- from a signed-off job: `POST .../validate` (from `done` or `done_with_warnings`) starts a model extraction over the job's
  translated segments. The pairs are checked by a second model pass, compared with the terms and rejected candidates
  already known, and the new ones are queued in `glossary_candidates` as `pending`;
- from the bulk import tooling of [utils/glossary](../utils/glossary/README.md).

A pending candidate is approved (it becomes a `glossary_terms` row), approved in bulk, or rejected with a reason.

## Cost

Every call of `call_llm_json` records its tokens in `translation_llm_calls` and adds them to the totals of the job.
The cost uses the per-endpoint prices of `server/services/llm.py`; an endpoint that is not listed is priced as a large
model, which overstates the cost until it gets its own entry.

## See also

- [lakebase_schema](lakebase_schema.md): the tables behind this.
- [translation engine](../server/services/translation/README.md): the modules that do the document work.
- [routers](../server/routers/README.md): the routes.
