"""Job file storage: UC Volume when TRANSLATE_VOLUME_PATH is set, local disk otherwise.

The local fallback lives in the app container's temp dir and survives only until the app restarts.
"""

import io
import os
import tempfile
from pathlib import Path

from databricks.sdk import WorkspaceClient


def job_root() -> str:
    """Return the root directory under which job files are stored.

    :return: The volume path when TRANSLATE_VOLUME_PATH is set, else TRANSLATE_LOCAL_STORAGE_DIR, else a temp dir; no trailing slash.
    """
    volume = os.getenv('TRANSLATE_VOLUME_PATH', '').strip().rstrip('/')
    if volume:
        return volume
    return os.getenv('TRANSLATE_LOCAL_STORAGE_DIR', '').rstrip('/') or str(Path(tempfile.gettempdir()) / 'latlang_jobs')


def _is_volume(path: str) -> bool:
    """Tell whether a path points into a UC Volume.

    :param path: Storage path.
    :return: True when the path starts with ``/Volumes/``.
    """
    return path.startswith('/Volumes/')


def upload(path: str, data: bytes) -> None:
    """Write bytes to a path, creating parent directories and overwriting any existing file.

    :param path: Destination, a UC Volume path or a local path.
    :param data: File content.
    """
    if _is_volume(path):
        w = WorkspaceClient()
        w.files.create_directory(path.rsplit('/', 1)[0])
        w.files.upload(path, io.BytesIO(data), overwrite=True)
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def download(path: str) -> bytes:
    """Read a file's content.

    :param path: Source, a UC Volume path or a local path.
    :return: The file content.
    """
    if _is_volume(path):
        resp = WorkspaceClient().files.download(path)
        content = resp.contents
        return content.read() if hasattr(content, 'read') else bytes(content)
    return Path(path).read_bytes()


def delete(path: str) -> None:
    """Remove a file; a file that is already gone is not an error.

    :param path: File to remove, a UC Volume path or a local path.
    """
    if _is_volume(path):
        try:
            WorkspaceClient().files.delete(path)
        except Exception as e:
            if 'not found' not in str(e).lower() and 'does not exist' not in str(e).lower():
                raise
        return
    Path(path).unlink(missing_ok=True)
