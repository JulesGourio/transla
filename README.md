# LatLang

LatLang is a Databricks App that translates Word (`.docx`) documents of aerospace documentation, mostly bilingual ones,
from one language to another while keeping the document's layout. A user uploads a document, picks the language to
replace and the language to replace it with, checks the result side by side with the original, corrects what needs it,
and downloads the translated `.docx`.

The app is standalone: its own FastAPI backend, React frontend, Databricks App and service principal, Lakebase database,
Unity Catalog schema and volumes. Nothing in it depends on another application at run time.

## What it does

| Step | What happens | Documentation |
|---|---|---|
| Upload | The document is checked, stored, and analysed: every paragraph (body, tables, text boxes, content controls, headers, footers, notes) is extracted, its language detected, bilingual pairs found | [docs/pipeline.md](docs/pipeline.md) |
| Questions | When a bilingual document has ambiguous segments, the user answers a few rule-generated questions | [docs/pipeline.md](docs/pipeline.md) |
| Translation | The segments to translate go to a Databricks model serving endpoint in batches, with the glossary and the do-not-translate rules | [docs/pipeline.md](docs/pipeline.md) |
| Rebuild | The translations are written back into the original XML, which keeps formatting; the result is validated | [docs/pipeline.md](docs/pipeline.md) |
| Review | Original and translation are shown side by side as PDFs; each difference can be edited, retranslated or accepted | [client/src/components/translate](client/src/components/translate/README.md) |
| Glossary | A reviewer maintains terms in eight languages and do-not-translate rules | [docs/pipeline.md](docs/pipeline.md) |

Supported languages: English, French, Spanish, German, Czech, Bulgarian, Portuguese, Arabic.

Optional at upload: translate the text found in chosen images of the document, and translate only a page range.

## Repository layout

```
server/       FastAPI backend: routes, translation engine, Lakebase, LibreOffice, model calls
client/       React and TypeScript frontend (Vite); client/out is the build the backend serves
docs/         Pipeline and database reference
utils/        Deployment script, Databricks notebooks and jobs helpers, glossary import tooling, LibreOffice packaging
resources/    Databricks job declared in the bundle
tests/        pytest suite
databricks.yml  Databricks Asset Bundle: app, schema, volumes, jobs, targets
app.yaml      Environment variables of the app, with their defaults
start.sh      Start command of the app
```

| Folder | README |
|---|---|
| `server/` | [server](server/README.md), then [routers](server/routers/README.md), [services](server/services/README.md), [translation engine](server/services/translation/README.md), [processors](server/services/processors/README.md) |
| `client/` | [client](client/README.md), then [src](client/src/README.md), [translate components](client/src/components/translate/README.md) |
| `docs/` | [docs](docs/README.md) |
| `utils/` | [utils](utils/README.md) |
| `resources/` | [resources](resources/README.md) |
| `tests/` | [tests](tests/README.md) |

Handing the project over: [HANDOVER.md](HANDOVER.md) (in French) lists what to know to operate, deploy and maintain the
application.

## Architecture

```
Browser ──> Databricks App "latlang" (FastAPI + built React)
              │   ├── Lakebase Postgres, database "latlang": jobs, segments, glossary, feedback
              │   ├── Unity Catalog volume "documents": originals, outputs, preview PDFs
              │   ├── Unity Catalog volume "binaries": portable LibreOffice archive
              │   └── Model serving endpoint (TRANSLATE_ENDPOINT): translation, image OCR, glossary checks
              │
Nightly job ──> copies every Lakebase table to Delta tables of the same schema
```

- The backend identifies the user from the `x-forwarded-user` header that Databricks Apps adds. A job belongs to the user
  who uploaded it; the glossary is shared by everyone. Who may open the app is decided by the permissions of the Databricks
  App, not by code.
- Long work runs as background tasks inside the app process. There is no queue; see
  [docs/pipeline.md](docs/pipeline.md#background-work) for how stuck jobs are detected.
- LibreOffice cannot be installed on Databricks Apps. A portable copy is stored as an archive in a volume, extracted once
  per process, and run as a subprocess to render the preview PDFs.

## Environments

| Target | Catalog | Schema | Notes |
|---|---|---|---|
| `dev` | `dev_proj` | `latlang` | Bundle target for development |
| `latlang-uat` | `uat_proj` | `latlang` | The deployed application |
| `latlang-prod` | `prod_proj` | `latlang` | Declared; the workspace host is not set yet |

In each schema: volumes `documents` (job files; the app reads and writes) and `binaries` (LibreOffice archive; the app
reads) and, once the nightly job has run, Delta copies of the Lakebase tables.

Lakebase: the project is shared with other applications (`LAKEBASE_PROJECT_ID`) to reuse its compute; this app owns the
database `latlang` inside it.

## Configuration

Two layers, the second wins:

1. `app.yaml`: defaults for every target.
2. `utils/deploy/target_env.json`: one object per target, rendered to `target_config.env` and sourced by `start.sh`.

| Variable | Default | Role |
|---|---|---|
| `LAKEBASE_PROJECT_ID` | `qualibot` | Lakebase project to connect to |
| `LAKEBASE_DATABASE` | `latlang` | Database in that project; created at startup when missing |
| `LAKEBASE_BRANCH`, `LAKEBASE_ENDPOINT` | `production`, `primary` | Branch and endpoint of the project |
| `TRANSLATE_ENDPOINT` | `databricks-gpt-5-6-luna` | Serving endpoint for translation and image OCR. Falls back to `COMPARE_ANALYSIS_ENDPOINT` when unset |
| `GLOSSARY_EXTRACT_ENDPOINT` | `databricks-gpt-5-4-mini` | Endpoint of the offline glossary tooling |
| `TRANSLATE_VOLUME_PATH` | per target | Root of the job files. When empty, the container's temporary disk is used and files are lost at restart |
| `TRANSLATE_MAX_CONCURRENT` | `3` | Model calls in flight, all jobs together |
| `TRANSLATE_MAX_CONCURRENT_PER_JOB` | `2` | Model calls in flight for one job |
| `TRANSLATE_MAX_CONCURRENT_JOBS` | `5` | Jobs processed at once |
| `TRANSLATE_BATCH_MAX_CHARS` | `6000` | Characters per translation batch |
| `TRANSLATE_ENDPOINT_TIMEOUT_S`, `TRANSLATE_ENDPOINT_CONNECT_TIMEOUT_S` | `300`, `30` | Model call timeouts |
| `MAX_TRANSLATE_FILE_MB`, `MAX_TRANSLATE_UNZIPPED_MB` | `60`, `800` | Upload size limits, as sent and once unzipped |
| `EUR_PER_USD` | `0.92` | Exchange rate of the cost estimate |
| `SOFFICE_ARCHIVE_VOLUME_PATH` | per target | Portable LibreOffice archive in a volume |
| `SOFFICE_PATH` | empty | Path of an existing `soffice` binary (local development) |
| `SOFFICE_EXTRACT_DIR` | temporary disk | Where the archive is extracted |
| `SOFFICE_MAX_CONCURRENT`, `SOFFICE_CONVERT_TIMEOUT_S` | `2`, `180` | Parallel conversions, and the time allowed for one |
| `APP_VERSION` | `1.0.0` | Version string logged at startup |
| `ENV` | not set | See [server](server/README.md#configuration) |

## Local development

Prerequisites: Python 3.11 or later, `uv` (or `pip`), `bun` (or `npm`), a Databricks login for Lakebase and the model
endpoint, and optionally LibreOffice (`SOFFICE_PATH`) for the PDF preview.

```bash
uv pip install -e ".[dev]"            # backend and test dependencies
cd client && bun install && bun run dev   # frontend on :3000, proxies /api to :8000
uvicorn server.app:app --reload       # backend on :8000
```

Put the settings you need in `.env.local` (loaded at startup, and it switches the app to development mode). Without
`TRANSLATE_VOLUME_PATH`, job files go to the temporary directory.

Tests: `python -m pytest` from the repository root, `cd client && bun run test` for the frontend. See [tests](tests/README.md).

## Deployment

```powershell
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat            # code only
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra     # also the bundle: app permissions, schema, volumes, jobs
```

What the script does, and the other Databricks steps, are in [utils/deploy](utils/deploy/README.md) and
[HANDOVER.md](HANDOVER.md).

## See also

- [docs](docs/README.md): pipeline and database reference.
- [server](server/README.md): the backend.
- [client](client/README.md): the frontend.
