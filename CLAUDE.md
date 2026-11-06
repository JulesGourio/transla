## Working setup (read first)

LatLang is a standalone Databricks App: own FastAPI backend, React frontend, Databricks App and service principal, one UC
schema per environment (`dev_proj.latlang`, `uat_proj.latlang`, `prod_proj.latlang`) with the volumes `documents` (job
files, app READ+WRITE) and `binaries` (LibreOffice archive, app READ), and its own Lakebase database `latlang` inside the
shared Postgres project `LAKEBASE_PROJECT_ID`. Pipeline: `docs/pipeline.md`. Schema: `docs/lakebase_schema.md`.

- **Claude has no Databricks access** (no CLI, workspace, UC or Lakebase). Never run or plan `databricks`/`bundle` commands
  yourself. Validate locally only (tests, build, YAML checks). Every step the user must run goes in the chat message, in
  full and in order (PowerShell, exact `bundle run` lines, SQL), and in `HANDOVER.md`, the single place for operating
  steps. Never answer "see section X" or with a file path instead of the commands.
- Code the user asks for is ready to paste: no placeholder to edit, no widget to fill in.
- **Deployment is manual for now**: `utils/deploy/deploy_latlang.ps1` with the user's own profile. There is no Bitbucket
  repository and no `job-runner-sa-<env>` deployment yet; do not write pipeline files that assume them.
- **Git: never open PRs.** Commit and push straight to `main`; a branch only for parallel work, merged by Claude.
- Chat in French. Code, identifiers, comments, commit messages in English.

## Databricks

- Bundle `latlang`; targets `dev`, `latlang-uat` (default), `latlang-prod` (workspace host not set yet).
- `latlang-uat` and `latlang-prod` are real. Never `bundle deploy` or `bundle destroy` on them without explicit approval,
  whatever any global policy says. DEV is the place to test.
- Prefer the bundle: `databricks bundle validate` -> `bundle plan -o json` -> `bundle deploy -t <target>`.
- On the user's machine the Databricks extension of the editor sets `DATABRICKS_TOKEN`, which silently overrides
  `--profile`: `Remove-Item Env:DATABRICKS_TOKEN` before any CLI call.
- `target_env.json` is the single source of the per-target app settings. The deploy script reads it: never copy its values
  elsewhere. A key set for one target is set for all (a test checks it). `LAKEBASE_DATABASE` is always set per target.
- Job format: `queue`, `email_notifications.on_failure`, `no_alert_for_canceled_runs` and the tags `activity-type`,
  `job-purpose`, `project-name` (`tests/test_deploy_config.py`).
- Grants never cover serving endpoints (`CAN_QUERY`): every workspace user can already query them.
- Lakebase: the project is shared with another application and is not declared in the bundle (a second bundle declaring
  it conflicts). The app's Postgres role is created by `utils/deploy/ensure_app_access.ps1` with `createdb` set from the
  start (`DATABRICKS_SUPERUSER` alone does not give it).

## Deployment rules that are easy to forget

- `requirements.txt` at the root is what Databricks Apps installs (not `pyproject.toml`); its versions are exact pins.
- Never declare `apps.*.resources` (`uc_securable`) in `databricks.yml`: Terraform then grants `USE CATALOG` to
  `account users` and fails without catalog `MANAGE`. The app's own service principal is granted by the
  `grant_volume_access` job, with `service_principal_client_id` (the `id` of `apps get`), not `oauth2_app_client_id`.
- A `resources.schemas|volumes|postgres_projects` block for an object another bundle (or target) already manages fails
  with "already exists" at deploy. `latlang-uat` shows the app as "not deployed" in `bundle summary` while it runs: check
  `bundle plan` before any `-Infra` deploy there.
- The frontend (`client/out`) is git-ignored: `sync.include` force-sends it, and `deploy_latlang.ps1` builds it with
  `bun install --frozen-lockfile`. Without it the app root answers 404 while `/api/*` works.
- `render_target_config_env.py` writes `target_config.env` with LF; `.gitattributes` keeps `*.sh` in LF. A CRLF adds a
  `\r` to every value in the Linux container.
- The grant job always gives READ and WRITE on the write volumes; a job created before the grant keeps a NULL
  `input_volume_path` and must be recreated.
- The bundle root is `/Workspace/Shared/.bundle/latlang/<target>`. Moving it moves the bundle state: it needs a copy of
  the state first.

## Code conventions (checked by `tests/test_code_conventions.py`)

- **Comments**: only the non-obvious *why*, one or two lines. No incident story, no date, no ticket, no commented-out code.
  The code stands alone: no comment, docstring, log or error message narrates a change ("before X, now Y", "fixed",
  "no longer", "regression", "used to"), refers to a PR, reviewer, bug, incident, run or runbook, or says where something
  is documented. Describe the behaviour as it is now. History belongs in git.
- **Docstrings and types**: every function and method, nested ones and notebook functions included, has a Sphinx
  docstring (one-line summary, `:param name:` for each parameter, `:return:` when it returns a value) and a return
  annotation. The docstring states the contract (unit, format, what `None` means), not how the body works.
- **Logging**: `logger = logging.getLogger(__name__)`, never `print`, scripts included. `%`-style lazy arguments.
- **Layout**: `server/routers/` one module per domain, the pipeline in `server/services/translation/`. A route module
  stays small; a job stage does not live in a router.
- **Notebooks** under `utils/` keep the Databricks source header, the logger and no `print`.
- No abstraction, config or error handling beyond what is asked.

## Documentation

- One README per folder that matters, in English, same shape: what the folder is, `Parent:` link, a file table, how it
  works, rules, `See also`. READMEs link to each other both ways.
- READMEs describe the code as it is now, present tense: no history, no dates, no incident story, no mention of an audit,
  a review, a bug fix, this file or an assistant.
- `HANDOVER.md` (French) is the operating guide. Update it in the same commit as any change of deployment, grants, jobs or
  configuration. A change to a route, a table, a setting or a stage updates the README of its folder, `docs/` and the
  settings table of the root README.
