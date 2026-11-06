"""FastAPI app of LatLang: the bilingual .docx translation pipeline and its React frontend."""

import asyncio
import logging
import os
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from .logging_setup import SLOW_REQUEST_S, is_quiet_path, request_id, setup_logging
from .routers import config, health, translate
from .services.lakebase import get_pool, init_lakebase, shutdown_lakebase, store_error

setup_logging()
logger = logging.getLogger(__name__)

env_local_loaded = load_dotenv(dotenv_path='.env.local')
env = os.getenv('ENV', 'development' if env_local_loaded else 'production')
logger.info(f'Starting in {env} mode')

_lakebase_id = os.getenv('LAKEBASE_PROJECT_ID', '')
_app_version = os.getenv('APP_VERSION', '1')
logger.info(f'LAKEBASE_PROJECT_ID = {_lakebase_id!r}')
logger.info(f'APP_VERSION         = {_app_version!r}')


async def _reconcile_orphaned_translation_jobs() -> None:
  """Marks as failed every job in an active stage whose worker heartbeat is older than two minutes.

  :return: None
  """
  pool = get_pool()
  if not pool:
    return
  active = ', '.join(f"'{s}'" for s in translate.ACTIVE_PROCESSING_STATUSES)
  try:
    async with pool.acquire() as conn:
      result = await conn.execute(f'''
        UPDATE translation_jobs
        SET status = 'failed',
            error_type = 'OrphanedJob',
            error_msg = 'Interrupted by app restart',
            updated_at = NOW()
        WHERE status IN ({active})
          AND (worker_heartbeat IS NULL OR worker_heartbeat < NOW() - INTERVAL '2 minutes')
      ''')
    logger.info(f'Translation job reconciliation: {result}')
  except Exception as e:
    logger.warning(f'Translation job reconciliation failed: {e}')


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
  """Opens the Lakebase pool and reconciles orphaned jobs at startup; closes the pool at shutdown.

  :param app: the FastAPI application
  :return: an async context manager yielded to FastAPI
  """
  await init_lakebase()
  await _reconcile_orphaned_translation_jobs()
  yield
  await shutdown_lakebase()


app = FastAPI(title='LatLang', version='1.0.0', lifespan=lifespan)

allowed_origins = ['http://localhost:3000'] if env == 'development' else []
app.add_middleware(
  CORSMiddleware,
  allow_origins=allowed_origins,
  allow_credentials=True,
  allow_methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE'],
  allow_headers=['Content-Type', 'Authorization'],
)


@app.middleware('http')
async def log_requests(request: Request, call_next) -> Response:
  """Tags the request with an id and logs mutations, slow requests and failures.

  :param request: incoming request
  :param call_next: the rest of the middleware chain
  :return: the response, carrying the ``X-Request-ID`` header
  """
  req_id = str(uuid.uuid4())[:8]
  token = request_id.set(req_id)
  start = time.monotonic()
  try:
    response = await call_next(request)
  except Exception:
    logger.error('%s %s failed after %.2fs', request.method, request.url.path, time.monotonic() - start, exc_info=True)
    raise
  finally:
    request_id.reset(token)
  elapsed = time.monotonic() - start
  response.headers['X-Request-ID'] = req_id
  mutates = request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
  if not is_quiet_path(request.url.path) and (mutates or elapsed >= SLOW_REQUEST_S or response.status_code >= 400):
    level = logging.WARNING if response.status_code >= 500 else logging.INFO
    logger.log(level, '[%s] %s %s -> %s (%.2fs)', req_id, request.method, request.url.path, response.status_code, elapsed)
  return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
  """Logs an unhandled exception, stores it in Lakebase and answers with a generic 500.

  :param request: the request that raised
  :param exc: the unhandled exception
  :return: a 500 response without any detail of the error
  """
  logger.error('Unhandled exception on %s %s: %s', request.method, request.url.path, exc, exc_info=True)
  asyncio.create_task(store_error(
      endpoint=f'{request.method} {request.url.path}',
      error_type=type(exc).__name__,
      error_msg=str(exc),
      stack_trace=traceback.format_exc(),
  ))
  return JSONResponse(status_code=500, content={'error': 'Internal server error'})

API_PREFIX = '/api'
app.include_router(health.router, prefix=API_PREFIX, tags=['health'])
app.include_router(config.router, prefix=API_PREFIX, tags=['config'])
app.include_router(translate.router, prefix=API_PREFIX, tags=['translate'])

# Serve Vite static build in production
build_path = Path('.') / 'client/out'
if build_path.exists():
  logger.info(f'Serving static files from {build_path}')
  app.mount('/assets', StaticFiles(directory=str(build_path / 'assets')), name='assets')

  for static_dir in ['images', 'logos', 'videos', 'content']:
    dir_path = build_path / static_dir
    if dir_path.exists():
      app.mount(f'/{static_dir}', StaticFiles(directory=str(dir_path)), name=static_dir)

  @app.get('/{full_path:path}')
  async def serve_spa(request: Request, full_path: str) -> FileResponse:
    """Serves a built frontend file, or index.html for any other non-API route.

    :param request: incoming request
    :param full_path: path below the site root
    :return: the file, never cached when it is index.html
    """
    file_path = build_path / full_path
    if full_path and file_path.is_file():
      return FileResponse(str(file_path))
    return FileResponse(
      str(build_path / 'index.html'),
      headers={'Cache-Control': 'no-cache, no-store, must-revalidate'},
    )
else:
  logger.warning(
    f'Build directory {build_path} not found. '
    'Run: cd client && bun run build'
  )
