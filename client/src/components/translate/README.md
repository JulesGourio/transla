# translate

The screens of LatLang: the upload form, the job status, the questions, the review of the translation, the segment table,
the glossary, the history, the shared read-only page, and the feedback form.

Parent: [src](../../README.md). What the backend does behind each button: [docs/pipeline.md](../../../../docs/pipeline.md)
and [routers](../../../../server/routers/README.md).

## Files

| File | Content |
|---|---|
| `TranslateView.tsx` | The page. Upload form, job polling, status panels, questions, action buttons, and the tabs of the finished job |
| `ReviewPanel.tsx` | The *Review* tab: both PDFs side by side, step through the differences, edit a translation in place |
| `reviewPanel/textMatch.ts` | Pure helpers of the review tab: locate a segment's text in the PDF text layer, map a scroll position from one document to the other. Unit-tested in `textMatch.test.ts` |
| `LayoutPreview.tsx` | The *Layout (PDF)* tab: both PDFs in the browser's own viewer, and the download buttons |
| `SegmentsPanel.tsx` | The segment table in a dialog: filter by category, edit, retranslate, change the language, sign the translation off |
| `GlossaryPanel.tsx` | The glossary dialog: terms, do-not-translate rules, candidates to review |
| `JobHistory.tsx` | The history drawer: the user's previous jobs, with status and counts, to reopen one |
| `FeedbackPanel.tsx` | Thumbs up or down with a comment on a job |
| `SharedTranslationPage.tsx` | The read-only page opened from a share link (`/shared/<token>`) |

## The page (`TranslateView.tsx`)

**Upload.** The user picks the source and target language (`en fr es de cs bg pt ar`), drops a `.docx`, and may turn on:

- *Also translate text found in images*: the file is sent to `POST /api/translate/analyze-images` and a grid of thumbnails
  lets the user choose the images to transcribe. Identical images are grouped with a repeat badge.
- *Pages to translate*: `3-10`, `1,4,9` or `1-3,7,12-15`.

**Status.** After the upload the page polls `GET /api/translate/jobs/{id}` every 2.5 seconds until the job is `done`,
`done_with_warnings` or `failed`. A poll that fails is tolerated; after four failures in a row the page says so. The job id is
kept in the address (`?job=123`), so a reload or a copied link returns to the same job.

The status area shows a stepper over the stages, cards for the counts and the language pair, and the model usage and cost.
When the job has reached `awaiting_answers`, the questions appear with their text, the context and a suggested answer.
Buttons then start the next step (translate, rebuild), and a failed job offers a retry.

**Warnings.** After a rebuild the page lists the segments that still read as the source language and those whose
translation may not fit its box. Each opens the segment table filtered on exactly those segments, and a segment can be
accepted as it is (*dismiss*).

**Actions** once a job exists: *History*, *Glossary*, *Segments*, download of the translated `.docx` or PDF, *Share* (copies
the link), *Feedback*, notes, and *Restart from scratch*. Restart opens a dialog that shows the images chosen before and
lets the user keep or change them; it wipes segments, questions and manual edits.

**Finished job.** Two tabs:

- *Review* ([ReviewPanel](#reviewpanel)).
- *Layout (PDF)*: the original and the translation in the browser's PDF viewer, for search, zoom and print.

The review tab also carries *Rebuild* and the option to add Word comments on flagged segments to the output.

## ReviewPanel

Renders both PDFs page by page with pdf.js (the worker is bundled by Vite), lazily, in two scroll columns of fixed height.

- A *difference* is a segment that was translated and changed. A switch walks through every segment instead, which is how
  a segment wrongly left untranslated is found.
- The toolbar shows the current segment (source with its language, translation with its status) and its position in the
  segment table. `n` and `p`, the buttons, or a number field move between segments.
- The toolbar edits the translation (`PATCH .../segments/{seg_id}`), retranslates it, or changes its language. An edit does
  not change the output document until *Rebuild*.
- The segment is located by searching its text in the pdf.js text layer (ignoring punctuation and case), starting from the
  expected page, and boxed in both documents. When it cannot be found, the columns scroll near the proportional position
  and no box is drawn; editing works either way because the segment id is known.
- *Linked scroll* keeps the two columns at the same relative position.

The segment table's *View in document* closes the dialog and jumps to a segment's difference here.

## SegmentsPanel

Tabs: *All*, *Translated*, *Kept (other lang)*, *Do-not-translate*, *Numeric*, *Outside page range*, *Needs review*, *Images*
and *Flagged* (any segment with a flag). A row shows the source, the translation, the language, the
status and the flag detail. A row can be edited, retranslated, have its language changed, or be shown in the document. An
image row shows its thumbnail, which opens larger on click. On a finished job the panel carries *Validate translation*,
which signs the document off and sends its term pairs to the glossary review.

## GlossaryPanel

Three lists: terms (search, add, delete, CSV export, definitions that expand), do-not-translate rules (pattern, type, match
mode, notes), and candidates to review (filtered by status; approve, reject with a reason, approve all). Terms and
candidates are paged by 50.

## Rules

- All text shown to the user is English.
- The pages call the API with `fetchJson`, which turns a non-2xx answer into an `Error` carrying the `error` field.
- A segment id is URL-encoded before it goes into a path, because it contains `/`.
- The page never assumes a status: after an action it fetches the job again, and the action buttons stay disabled until
  that first fetch lands.

## See also

- [src](../../README.md): the shared code (`lib`, theme, layout, flags).
- [routers](../../../../server/routers/README.md): the routes these components call.
