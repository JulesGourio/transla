# translation

The translation engine: everything that reads a `.docx`, decides what to translate, writes the translation back, and
checks the result. The modules work on bytes and dictionaries and never touch the database; the
[router](../../routers/README.md) drives them through the [processors](../processors/README.md) adapter.

Parent: [services](../README.md). Pipeline walk-through: [docs/pipeline.md](../../../docs/pipeline.md).

## Files

| Module | Content |
|---|---|
| `extract.py` | Reads the XML parts of a `.docx` and emits one segment per translatable paragraph |
| `langdetect.py` | Language detection for `en fr es de cs bg pt ar`, do-not-translate recognition, numeric-only recognition |
| `audit.py` | Per-segment classification, document mode, bilingual pairing, inline splits, conflict detection, questions |
| `glossary_io.py` | `DntMatcher` (the reviewer's do-not-translate rules) and `dnt_rule_problem` (rejects a rule that matches everything or nothing) |
| `glossary_lakebase.py` | `generate_term_id`: the next `T####` glossary id, allocated under an advisory lock |
| `glossary_extract.py` | Extracts term-pair candidates from the translated segments of a job, with a model |
| `glossary_verify.py` | `llm_verify`: a model pass that rejects extraction artifacts and normalises casing in candidate pairs |
| `fit_check.py` | Overflow risk per container type |
| `length_adapt.py` | Abbreviation rules that shorten a translation |
| `rebuild.py` | Writes the translations back into the XML and the zip |
| `validate.py` | The structural checks and the residual-language check (the per-file checks; the bytes versions are in the processors) |
| `comments.py` | Injects Word comments into a `.docx` |
| `review_comments.py` | Turns a job's flags into the comment specifications `comments.py` injects |
| `docx_images.py` | Lists, groups and transcribes the images of a `.docx`, and orders the resulting segments |
| `pages.py` | Page specification parsing and the segment-to-page mapping |
| `pdf_diff.py` | Pixel difference between two PDFs (PyMuPDF and Pillow) |

Several modules still carry a command-line `main()`; the application never calls it.

## extract.py

`discover_text_parts` lists the parts that can hold text, in a stable order: `word/document.xml`, headers, footers (sorted
by number), footnotes, endnotes, comments. For each part the module walks the tree and emits segments for body
paragraphs, tables, drawing and VML text boxes, and content controls, descending into nested ones.

- A segment id is built from the part and the position (`word/document.xml#table.bt5.r5.c0#p0`). Two segments that would
  get the same id are made unique.
- `positional_path` returns `[[tag, child_index], ...]` from the part root to the paragraph; `rebuild.walk_path` is its
  exact inverse.
- A drawing text box lives under `mc:AlternateContent` twice (`mc:Choice` and `mc:Fallback`). The extraction goes through
  the `Choice` copy and records the matching `Fallback` path, so the rebuild updates both copies.
- Every segment carries a formatting fingerprint of its runs (`fmt_hash` per run, one signature per paragraph) that the
  audit uses to tell the two languages of a mixed paragraph apart, and the list of its runs.

## langdetect.py

- `detect_language` is a heuristic with ordered tiers: the Cyrillic block (Bulgarian), the Arabic block (Arabic),
  characters that belong to one language (`ř ů` Czech, `ß` German, `œ` French, `ñ ¿ ¡` Spanish, `ã õ` Portuguese), shared
  diacritics, then stopword scores. A tier needs a ratio or an absolute number of hits, so a short mixed text is not
  outvoted by character counts. No signal returns `("??", 0.0)`.
- `detect_language_constrained(text, candidates, source_lang)` is what the audit uses. It runs the heuristic and
  fastText `lid.176` renormalised over the candidate languages. Only agreement gives a confidence of 0.85 or more; a
  single detector is capped at 0.80, because fastText is sometimes confidently wrong on short titles that read the same in
  two languages.
- `detect_span_language` labels one side of a split text.
- `DNT_REGEX` recognises fastener and part numbers, internal references (including ones that start with a digit), standards
  codes and two date formats. `extract_dnt_tokens` returns the tokens of a text; `is_dnt_candidate` says whether the whole
  text is one; `is_numeric_only` whether it is only numbers.
- `normalize_hyphens` maps non-breaking and other hyphens to `-`, so a token with a non-breaking hyphen still matches.
- The fastText model is `server/data/lid.176.ftz`. Without it, the heuristic runs alone.

## audit.py

`audit_all_segments` classifies each segment (`dnt`, `numeric_only`, else `mono` with a language), in two passes: the first
over all supported languages to learn which ones the document uses, the second constrained to the declared pair plus
those. Segments with no signal then take the dominant language. `determine_mode` returns `bilingual` or `monolingual`.

Pairing, in this order, each segment joining at most one pair:

| Pattern | Rule |
|---|---|
| `bilingual_txbx_pair` | Two consecutive paragraphs of one text box in different languages |
| `paired_body` | Two consecutive body paragraphs in different languages |
| `paired_row` | Two adjacent table rows, each uniformly in one language |
| `bilingual_inline_slash` | One segment `X / Y` whose halves are in different languages |
| `bilingual_inline_concat` | One segment whose runs fall into two formatting groups in different languages |
| `bilingual_inline_charsplit` | One segment with a script switch or a sentence boundary between the languages |

`assess_pair` compares the two sides of a pair (numbers, reference codes, length ratio) and sets the conflict flag.
`generate_questions` returns the `unknown_lang` questions of a bilingual document and nothing for a monolingual one.

## fit_check.py and length_adapt.py

A translation is compared with its source by container: a header or a footer is `CRITICAL` as soon as it grows, a text box
is flagged above 130 % of the source length, a table cell above 150 %, a body paragraph above 200 %. `length_adapt.shorten`
applies regular-expression shortening rules chosen by target language (French, English, Spanish, German, Czech); another
language gets no rule and is left unchanged.

## rebuild.py

Opens the original as a zip, rewrites each text part, and copies every other entry byte for byte.

- lxml parses and serialises the parts; `merge_root_tags` restores the original root tag from the raw bytes, so every
  namespace declaration (including ones only `mc:Ignorable` references) and its prefix stay as they were. Word refuses a
  file where they changed.
- The paragraph is found by `walk_path`. How the text is written depends on the segment: `replace_uniform` puts the
  translation in the first run and empties the others; `replace_bilingual_format_split` replaces only the runs of the
  source formatting group; `replace_slash_split` replaces one side of `X / Y`; `replace_proportional` spreads text
  across runs by length when formats differ.
- A control character that XML forbids is dropped from a translation instead of failing the rebuild.

## validate.py

The per-file checks `check_zip_integrity`, `check_xml_validity`, `check_namespace_integrity`, `check_auto_prefixes`,
`check_content_types`, plus fidelity, byte identity and residual language for a file on disk. The application runs the
bytes versions in [processors](../processors/README.md).

## comments.py and review_comments.py

`generate_review_comments` builds comment specifications from signals the pipeline already holds: `conflict_detail`, the
segment category, the inline pattern, and the fit-check list. `inject_comments` adds the four parts Word needs for a
comment (`word/comments.xml`, the range and reference markers in the paragraph, the relationship, the content type) and
merges with comments already in the document. A specification whose segment is unknown is skipped.

## docx_images.py

- `list_docx_images` lists `word/media` rasters with a thumbnail, grouped by content hash with a repeat count.
- `ocr_docx_images` sends one vision request per distinct image (PNG or JPEG as is, other formats converted to PNG) and
  fans each transcription out to every file with the same hash.
- `find_image_anchors` finds, for each image, the index of the body child that holds its drawing; `order_ocr_segments`
  inserts the image segments into the extracted list at that position. The body index in an image segment stays `-1`, so
  the pairing in `audit.py` never takes it for a neighbour.
- `insert_image_translations` writes an italic `[Traduction]` paragraph after an image. The pipeline does not call it;
  image translations are shown in the application only.

## Rules

- A function here takes and returns plain data; it opens no database connection.
- A segment id is a position. Inserting a paragraph into the body shifts every later id, which is why `rebuild` never adds
  paragraphs and `segment_fidelity` compares ids.
- The stable parts of a document (styles, settings, theme, media, ...) are never rewritten.
- Supported languages are `en fr es de cs bg pt ar`. Adding one touches `langdetect.LANG_PROFILES`, `SUPPORTED_LANGS`, the
  language name tables in `routers/translate.py` and `glossary_verify.py`, the glossary columns, and the client's language
  lists.

## See also

- [processors](../processors/README.md): how the router calls these modules.
- [docs/pipeline.md](../../../docs/pipeline.md): the order of the stages.
- [tests](../../../tests/README.md): the tests that pin the behaviour above.
