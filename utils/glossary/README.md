# glossary

Command-line scripts that mined a document base for glossary terms and loaded them into Lakebase. They are bootstrap
tooling: the application does not import them, and the glossary now grows from the application itself (see
[docs/pipeline.md](../../docs/pipeline.md#glossary)).

Parent: [utils](../README.md).

## What they rely on

- The Databricks CLI, logged in, and a SQL warehouse. `_sql.py` holds the defaults: the CLI profile `UAT`, a serverless
  warehouse id, and the name of the chunks table of the document base (`CHUNKS_TABLE`). That table belongs to another
  application's document base and is not created here; change the three constants before running anything against another
  workspace.
- Documents published in several languages under one reference with a language suffix (`IF20335_FR`, `IF20335_GB`,
  `IF20335_BG`). Two versions of the same document, aligned section by section, give professional term pairs.
- A model serving endpoint: `GLOSSARY_EXTRACT_ENDPOINT`, default `databricks-gpt-5-4-mini`.
- For the scripts that write: access to the Lakebase database, through `--env` (`DEV`, `UAT`, `UAT-TEST`). The values name
  workspaces and databases of an earlier layout of the project; check what each one points at in the script before using
  `--apply`.
- Candidate files are CSV with `;` as the separator. The scripts look for them in a `Translator/glossary/candidates`
  folder, which is git-ignored and not part of the repository.

The candidate files have one column per language (`en fr cs bg de es`), plus `n_docs`, `definition`,
`definition_source`, `sources` and `status`. Portuguese and Arabic are not part of these scripts: the document base held
only the first six languages.

## Files

| Script | Does |
|---|---|
| `_sql.py` | Runs a SQL statement on the warehouse through the CLI |
| `find_multilingual_pairs.py` | Lists the families of documents published in several languages |
| `extract_glossary_candidates.py` | One family: aligns the chunks of two languages (by relative position and shared anchors such as part numbers, standards, numbers) and, with `--llm`, extracts term pairs |
| `build_review_batch.py` | Every family with the requested language pair: align, extract, merge (a term seen in N families has confidence N), remove what the glossary already has, write one CSV sorted by confidence |
| `find_index_sections.py` | Finds the documents that contain a terminology, glossary or acronym section |
| `extract_index_terms.py` | Extracts the entries of such a section for one document |
| `extract_index_terms_bulk.py` | The same for every document found; resumable (a document with a CSV is skipped) |
| `cross_lang_definitions.py` | Gives a pair a corpus definition (`definition_source = cross_lang_pair`) when the same term has an authentic definition in another language |
| `make_review_shortlist.py` | Merges the review batches into one shortlist ordered by confidence |
| `import_reviewed_candidates.py` | Loads rows whose `status` is an approval into `glossary_terms`; skips terms already there. Dry run unless `--apply` |
| `sync_candidates_to_lakebase.py` | Removes duplicates of the glossary, checks each pair with a model (`llm_verify`), and inserts the survivors straight into `glossary_terms`. Dry run unless `--apply` |
| `promote_pending_candidates.py` | One-time cleanup: checks the `glossary_candidates` rows that were pending and promotes the good ones. Dry run unless `--apply` |

## Order

1. `find_multilingual_pairs` -> `build_review_batch --langs fr en` for each pair (and, for terminology sections,
   `find_index_sections` -> `extract_index_terms_bulk` -> `cross_lang_definitions`).
2. `make_review_shortlist`.
3. `sync_candidates_to_lakebase` (model-checked, direct insert) or, after a human review, `import_reviewed_candidates`.

`llm_verify` and the term id allocation live in the application package
([glossary_verify.py](../../server/services/translation/glossary_verify.py),
[glossary_lakebase.py](../../server/services/translation/glossary_lakebase.py)) and are shared with the application.

## Rules

- Every script that writes is a dry run until `--apply`.
- A pair is checked for extraction artifacts before it is inserted: a document heading taken for a term, a sentence
  fragment, two unrelated words paired by a misaligned chunk. Casing is normalised: lower case for a common noun, original
  case kept for proper nouns and acronyms. A batch the model call fails on is kept as it is.

## See also

- [services/translation](../../server/services/translation/README.md): the shared helpers.
- [docs/lakebase_schema.md](../../docs/lakebase_schema.md): `glossary_terms` and `glossary_candidates`.
