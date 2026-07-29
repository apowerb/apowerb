"""RAG router package — aggregates endpoint sub-modules into a single APIRouter.

Consumers (``main.py`` and tests) import ``router`` from this package.  The
top-level symbols re-exported here (``append_source``, ``rag_manager``,
``_sync_index_db``, ``_sync_index_db_nl``) preserve the pre-split public
patching surface: ``unittest.mock.patch("th2agent.routers.rag.<name>", ...)``
still targets the same attribute, and every endpoint resolves the name via
``th2agent.routers.rag`` so the patch takes effect.
"""

from fastapi import APIRouter

# Re-export patchable module-level symbols for backwards-compatible test mocking.
from th2agent.core.knowledge_map import append_source  # noqa: F401
from th2agent.core.rag_streaming import rag_manager  # noqa: F401

from .index_db import _sync_index_db, _sync_index_db_nl  # noqa: F401
from .index_db import router as _index_db_router
from .index_files import router as _index_files_router
from .index_s3 import router as _index_s3_router
from .index_url import router as _index_url_router
from .schemas import (  # noqa: F401
    DbCredentials,
    IndexDbNlRequest,
    IndexDbRequest,
    IndexS3Request,
    IndexUrlRequest,
    WebhookPayload,
)
from .status import router as _status_router
from .stream import router as _stream_router
from .webhook import router as _webhook_router

router = APIRouter()
router.include_router(_index_files_router)
router.include_router(_index_url_router)
router.include_router(_index_db_router)
router.include_router(_index_s3_router)
router.include_router(_status_router)
router.include_router(_webhook_router)
router.include_router(_stream_router)

__all__ = [
    "router",
    "append_source",
    "rag_manager",
    "_sync_index_db",
    "_sync_index_db_nl",
    "DbCredentials",
    "IndexDbNlRequest",
    "IndexDbRequest",
    "IndexS3Request",
    "IndexUrlRequest",
    "WebhookPayload",
]
