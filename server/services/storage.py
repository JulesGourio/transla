"""Job file storage: UC Volume when TRANSLATE_VOLUME_PATH is set, local disk otherwise.

Local fallback lives in the app container's temp dir — files survive only
until the app restarts, but the pipeline (rebuild/validate/preview) keeps working.
"""

import io
import os
import tempfile
from pathlib import Path

from databricks.sdk import WorkspaceClient


def job_root() -> str:
    volume = os.getenv('TRANSLATE_VOLUME_PATH', '').strip().rstrip('/')
    if volume:
        return volume
    return os.getenv('TRANSLATE_LOCAL_STORAGE_DIR', '').rstrip('/') or str(Path(tempfile.gettempdir()) / 'latlang_jobs')


def _is_volume(path: str) -> bool:
    return path.startswith('/Volumes/')


def upload(path: str, data: bytes) -> None:
    if _is_volume(path):
        w = WorkspaceClient()
        w.files.create_directory(path.rsplit('/', 1)[0])
        w.files.upload(path, io.BytesIO(data), overwrite=True)
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def download(path: str) -> bytes:
    if _is_volume(path):
        resp = WorkspaceClient().files.download(path)
        content = resp.contents
        return content.read() if hasattr(content, 'read') else bytes(content)
    return Path(path).read_bytes()
