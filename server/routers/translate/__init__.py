"""Translate routes: jobs, pipeline stages, questions, segments, previews, sharing, feedback and glossary.

The sub-routers are merged into a single `router` mounted by the application; every route keeps its full
`/translate/...` path.
"""

from fastapi import APIRouter

from ...services.translation.jobs_db import ACTIVE_PROCESSING_STATUSES
from . import glossary, jobs, preview, questions, segments, share_feedback, stages

router = APIRouter()
for _sub in (jobs, stages, questions, segments, preview, share_feedback, glossary):
    router.include_router(_sub.router)

__all__ = ['ACTIVE_PROCESSING_STATUSES', 'router']



