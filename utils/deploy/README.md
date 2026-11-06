# deploy

Deploys the application to a Databricks workspace.

Parent: [utils](../README.md).

## Files

| File | Content |
|---|---|
| `deploy_latlang.ps1` | The deployment script |
| `ensure_app_access.ps1` | Creates, when missing, the Postgres role of the app's service principal on the shared Lakebase project |
| `render_target_config_env.py` | Writes `target_config.env` for one target from `target_env.json` |
| `target_env.json` | The settings that differ per target: `LAKEBASE_DATABASE`, `TRANSLATE_VOLUME_PATH`, `SOFFICE_ARCHIVE_VOLUME_PATH` |

## deploy_latlang.ps1

```powershell
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat              # code only
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -SkipBuild   # code only, frontend not rebuilt
.\utils\deploy\deploy_latlang.ps1 -AppEnv uat -Infra       # bundle deploy first
```

`-AppEnv` is `dev` (bundle target `dev`, CLI profile `DEV`), `uat` (bundle target `latlang-uat`, CLI profile `UAT`) or `prod` (`latlang-prod`, profile `qualibot-prod`). The
app is named `latlang` in both.

Steps:

1. Build the frontend (`bun install --frozen-lockfile`, `bun run build`), unless `-SkipBuild`.
2. Render `target_config.env`, then either run `databricks bundle deploy` (`-Infra`) or `databricks sync` the working
   folder to `/Workspace/Shared/.bundle/latlang/<target>/files`. The sync force-includes `target_config.env` and
   `client/out` (both are git-ignored) and leaves out the frontend sources.
3. Start the app compute when it is stopped, because a deployment to a stopped app is refused.
4. `databricks apps deploy`, which restarts the app on the uploaded files.

`-Infra` applies the bundle: app permissions, the schema and volumes, the jobs. The first deployment of a target needs
`MANAGE` on the target catalog. A routine code change does not need it.

## ensure_app_access.ps1

```powershell
.\utils\deploy\ensure_app_access.ps1 -AppEnv dev     # dev | uat
```

Reads the service principal of the app `latlang`, looks for its role on `projects/qualibot/branches/production` and creates
it with `createdb` when absent. Run it once after the first deployment of a target, then restart the app.

## target_env.json

An object per target. Each key becomes `export KEY='value'` in `target_config.env`, which `start.sh` sources before it
starts the server, so it overrides the default of `app.yaml`. The renderer writes LF line endings: with CRLF, bash keeps a
`\r` at the end of every value and, for instance, the database name no longer matches.

A new per-target setting is added here only; the script and the pipeline read this file and nothing else.

## Rules

- Run `Remove-Item Env:DATABRICKS_TOKEN` first on a machine where the Databricks extension of the editor sets that
  variable: it overrides the profile.
- Never deploy or destroy `latlang-uat` or `latlang-prod` without the owner's approval.
- `LAKEBASE_DATABASE` is set in `target_env.json` for every target, so a deployment cannot start on a database meant for
  another one.

## See also

- [root README](../../README.md#configuration): the settings.
- [resources](../../resources/README.md): what `-Infra` deploys besides the app.
- [HANDOVER.md](../../HANDOVER.md): the full deployment procedure, with the commands around it.
