# processors

The adapter between the translate router and the translation engine. The engine modules read and write files; the router
holds uploaded bytes. `translation.py` composes the engine against bytes in memory.

Parent: [services](../README.md). Engine: [translation](../translation/README.md).

## Files

| Function of `translation.py` | What it does |
|---|---|
| `extract_docx_segments(bytes)` | Segments of a document, from the engine's `extract` |
| `audit_segments(segments, dnt_rows, source, target)` | Language detection constrained to the pair, mode, pairing, conflicts and questions. Returns `(report, questions)`. Builds a `DntMatcher` from the rows for each call, so two jobs never share rules. Pairing across paragraphs and rows runs only in bilingual mode; inline splits run in both modes |
| `check_fit(extract, rows)` | Segments likely to overflow, and the number of `CRITICAL` ones |
| `adapt_lengths(extract, rows, target)` | Shortens flagged translations; returns the updated rows and the suggestions |
| `build_rebuild_inputs(rows)` | Groups the rows by XML part and builds the metadata the rebuild needs |
| `rebuild_docx_bytes(src, by_part, metadata)` | The rebuilt document as bytes |
| `find_unplaced_translations(rebuilt, rows)` | Ids of segments whose translation did not land in the document |
| `validate_docx(rebuilt, original, reference_segments, source_lang)` | The validation suite; see [pipeline](../../../docs/pipeline.md#7-rebuild) |

`validate_docx` reimplements three checks on bytes (segment fidelity, byte identity, residual language) and reuses the
engine's five zip and XML checks as they are. Segment fidelity ignores `word/comments.xml` and the marker paragraphs of
image translations. The residual-language check works by script for Bulgarian and Arabic (a paragraph counts when most of
its words are written in that script, so a code with a look-alike letter does not), by characters that exist only in the
source language where there are some, and otherwise by the constrained language detector at 0.55 confidence or more.
`adapt_lengths` changes the rows it is given.

## Rules

- No function here reads or writes a file on disk.
- No function here calls a model.

## See also

- [translation](../translation/README.md): the modules composed here.
- [routers](../../routers/README.md): the only caller.
