# LatLang

Bilingual `.docx` translation pipeline for Latecoere aerospace documentation,
running as its own standalone Databricks App. See
`client/src/components/translate/README.md` for the full pipeline
documentation (extract → audit → Q&A → translate → fit_check → rebuild →
validate → preview) and the Lakebase schema.

## Independence

Fully standalone app: own FastAPI backend, own React frontend, own Lakebase
database (`latlang`), own catalog schema and volumes, own
Databricks App and service principal. `LAKEBASE_PROJECT_ID` reuses an
existing shared Lakebase Postgres project id to avoid provisioning new
compute — that's the only shared infra, and this app owns its own database
inside it. No runtime dependency on any other app.

## Local development

```bash
uv pip install -e .
cd client && bun install && bun run dev   # or npm install / npm run dev
```

Backend: `uvicorn server.app:app --reload`

## Deployment

```powershell
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat          # code only
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra   # + schema, volumes, jobs
```

Unity Catalog layout, one-time setup steps and every other Databricks
command: `OPS_COMMANDS.md`. Never `deploy`/`destroy` on `latlang-uat` or
`latlang-prod` without explicit approval.

## Open items

- Create the `Role-Project-LEAP-End-users-LatLang` Databricks group (or agree
  on its final name) and grant it access in `databricks.yml`.
- Decide whether to copy the historical glossary/DNT data from the
  `latlang_test` Lakebase database into `latlang`
  (`utils/databricks_ops/lakebase_sync/migrate_translation_data_from_doccompare.py`
  is the earlier one-time importer).
- `latlang-prod`: workspace host still empty, never deployed.
