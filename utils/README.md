# utils

Tools that run outside the application: deployment, Databricks notebooks and job helpers, glossary import tooling, and the
packaging of LibreOffice. One folder per concern.

Parent: [root README](../README.md).

## Folders

| Folder | Content | README |
|---|---|---|
| `deploy/` | The deployment script and the per-target settings it renders | [deploy](deploy/README.md) |
| `databricks_ops/` | Notebooks run as Databricks jobs: copy Lakebase to Unity Catalog, grant volume access, start and stop the app, and the grant files | [databricks_ops](databricks_ops/README.md) |
| `export_lakebase/` | A parameterised copy of the Lakebase to Unity Catalog notebook | [export_lakebase](export_lakebase/README.md) |
| `glossary/` | Scripts that mined a document base for glossary terms | [glossary](glossary/README.md) |
| `soffice_packaging/` | Builds the portable LibreOffice archive the app extracts | [soffice_packaging](soffice_packaging/README.md) |

## Rules

- A file that starts with `# Databricks notebook source` is a job task or a notebook opened in Databricks. It does not run
  from the command line.
- A command-line script is run from the repository root, as a module when it lives in a package
  (`python -m utils.glossary.<name>`).
- `utils/deploy/target_env.json` is the only place that holds the per-target application settings.

## See also

- [resources](../resources/README.md): the jobs that run these notebooks.
- [HANDOVER.md](../HANDOVER.md): how and when each one is used.
