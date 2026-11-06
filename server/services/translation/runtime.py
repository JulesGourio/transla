"""Process-wide runtime pieces shared by the detached translation stages: task retention, the active-job cap and
the service-principal credentials."""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


# asyncio keeps only a weak reference to a running task: a fire-and-forget one
# can be garbage-collected mid-run (the job then sits in its active status until
# the stale-heartbeat reconciliation fails it).
background_tasks: set = set()


def spawn(coro) -> 'asyncio.Task':
    """Run a coroutine as a background task that stays referenced until it finishes.

    :param coro: coroutine to schedule on the running event loop.
    :return: the scheduled task.
    """
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


# Caps how many jobs can be actively processed (extracting/auditing/translating/
# fit_checking/rebuilding/validating) at once across all users in this process —
# protects the default asyncio thread-pool executor and the Lakebase pool from
# an unbounded number of concurrent CPU-bound stages. Upload itself is never
# blocked by this — only the background processing of a job waits its turn.
ACTIVE_JOBS_SEMAPHORE = asyncio.Semaphore(int(os.getenv('TRANSLATE_MAX_CONCURRENT_JOBS', '5')))


def get_llm_credentials() -> tuple[str, str]:
    """Resolve the workspace host and bearer token for a task that has no request context.

    :return: (host, token); host is an https URL without trailing slash, and either value is '' when unavailable.
    """
    host = os.environ.get('DATABRICKS_HOST', '').rstrip('/')
    if host and not host.startswith('http'):
        host = f'https://{host}'
    token = os.environ.get('DATABRICKS_TOKEN', '')
    if not host or not token:
        try:
            from databricks.sdk.core import Config
            cfg = Config()
            if not host:
                host = (cfg.host or '').rstrip('/')
            if not token:
                auth_value = cfg.authenticate().get('Authorization', '')
                if auth_value.startswith('Bearer '):
                    token = auth_value[len('Bearer '):]
        except Exception as e:
            logger.debug('SDK auth unavailable: %s', e)
    return host, token
