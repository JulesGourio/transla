"""Health endpoints: liveness for the platform probe, readiness for a person checking a deployment."""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..services import storage
from ..services.lakebase import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get('/health')
async def health_check() -> dict[str, Any]:
  """Liveness probe: answers as long as the process serves requests.

  :return: status and a millisecond timestamp
  """
  return {'status': 'healthy', 'timestamp': int(time.time() * 1000)}


@router.get('/health/ready')
async def readiness_check() -> JSONResponse:
  """Readiness probe: checks that Lakebase answers and that the job storage is writable.

  :return: 200 with the status of each dependency, or 503 when one is down
  """
  checks: dict[str, str] = {}
  pool = get_pool()
  if pool is None:
    checks['lakebase'] = 'not configured'
  else:
    try:
      async with pool.acquire() as conn:
        await conn.fetchval('SELECT 1')
      checks['lakebase'] = 'ok'
    except Exception as exc:
      logger.warning('Readiness: Lakebase unreachable: %s', exc)
      checks['lakebase'] = 'unreachable'
  try:
    await asyncio.to_thread(storage.upload, f'{storage.job_root()}/.readiness_probe', b'ok')
    checks['storage'] = 'ok'
  except Exception as exc:
    logger.warning('Readiness: storage not writable: %s', exc)
    checks['storage'] = 'not writable'
  ready = all(v == 'ok' for v in checks.values())
  return JSONResponse(status_code=200 if ready else 503, content={'status': 'ready' if ready else 'degraded', 'checks': checks})
