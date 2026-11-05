# server

The FastAPI backend of LatLang. It serves the `/api` routes, runs the translation pipeline, and serves the built frontend
from `client/out`. It keeps no data of its own: jobs and glossary live in Lakebase, files in a Unity Catalog volume.

Project overview: [root README](../README.md).

## Files

| Path | Content |
|---|---|
| `app.py` | Entry point (`server.app:app`): logging, startup (Lakebase, orphaned jobs), CORS, request log, the routers under `/api`, static files and the single-page fallback |
| [`routers/`](routers/README.md) | The HTTP routes: `translate` (the whole feature), `config`, `health` |
| [`services/`](services/README.md) | The logic behind the routes: Lakebase, model calls, LibreOffice, storage, user identity, and the translation engine |
| `data/lid.176.ftz` | The fastText language-identification model used by the language detector. The folder has no README |

## How the app starts

`app.yaml` runs `bash start.sh`. The script makes sure `HOME` is set, sources `target_config.env` when that file exists,
sets `TZ=Europe/Paris`, and runs `uvicorn server.app:app`.

At startup `app.py`:

1. opens the Lakebase pool, creates the database when it is missing, and creates or upgrades the tables
   ([services](services/README.md#lakebasepy));
2. marks as failed the jobs a previous process left in an active state (see
   [docs/pipeline.md](../docs/pipeline.md#background-work)).

At shutdown it closes the Lakebase pool. When `client/out` exists, `app.py` mounts its `assets`, `images`, `logos`,
`videos` and `content` folders, and answers every other path outside `/api` with the matching file or with `index.html`.
Without `client/out` it logs a warning and serves only the API.

## Configuration

Two sources; the second one wins:

1. `app.yaml`, the `env:` list: the default of every setting, for every target.
2. `utils/deploy/target_env.json`: one object per target, rendered to `target_config.env` (LF line endings) and sourced by
   `start.sh`. A setting missing for a target keeps its `app.yaml` default.

Locally, `app.py` loads `.env.local` when it exists. The variables are listed in the [root README](../README.md#configuration);
each module reads the ones it uses.

`ENV` is set nowhere in the repository. `app.py` then takes `production` (no CORS origin), while
`services/user.py` takes `development` (its default), which lets a request without an `x-forwarded-user` header act as the
account of the SDK credentials. Behind Databricks Apps every request has the header, so the difference is invisible there.

## Rules

- Every route is under `/api`. The single-page fallback is registered after the routers, so an `/api` path never returns
  `index.html`.
- Each request gets an 8-character id, written in the log lines and returned in the `X-Request-ID` header.
- An unhandled exception is stored in the `errors` table and answered with `500 {"error": "Internal server error"}`.
- A route returns `503` when Lakebase is not configured, instead of failing.
- Blocking work (file parsing, conversions, Pillow, PyMuPDF, SDK calls) runs in a thread through `asyncio.to_thread`.
- Log writes and optional steps are best effort: they log their own failure and never fail the request or the job.

## See also

- [routers](routers/README.md): the HTTP routes.
- [services](services/README.md): the logic behind them.
- [docs/pipeline.md](../docs/pipeline.md): what a job does from upload to download.
