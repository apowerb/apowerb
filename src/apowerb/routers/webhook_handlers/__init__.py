"""Webhook notification handlers -- one module per service.

Each handler exposes an async ``handle_*_notification(request, background_tasks, **kwargs)``
callable that is registered in ``NOTIFICATION_HANDLERS`` and dispatched by
the unified ``POST /api/webhooks/{service}/notifications`` endpoint in
``routers/webhooks.py``.

To add a new service (e.g. OneDrive, Teams):
    1. Create ``webhook_handlers/onedrive.py`` with a ``handle_onedrive_notification`` function.
    2. Register it in ``NOTIFICATION_HANDLERS`` below.
    3. That's it -- the dispatch endpoint picks it up automatically.
"""

from .gmail import handle_gmail_notification
from .outlook import handle_outlook_notification

# Registry: maps the ``{service}`` path segment to its async handler.
# Each handler signature: (request, background_tasks, **kwargs) -> Response
NOTIFICATION_HANDLERS = {
    "outlook": handle_outlook_notification,
    "gmail": handle_gmail_notification,
}

__all__ = [
    "NOTIFICATION_HANDLERS",
    "handle_gmail_notification",
    "handle_outlook_notification",
]
