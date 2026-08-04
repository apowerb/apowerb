"""Artifact tools.

This category is a package, not a flat module like its siblings
(``basic.py``, ``database.py``, ...). The catalogue imports the category and
collects its ``tool_*`` functions with ``inspect.getmembers``, so anything not
re-exported here stays invisible to agents — which is precisely why the
artifact-saving tool was unreachable until 2026-08-04.
"""

from .save_code_artifact import tool_save_code_artifact

__all__ = ["tool_save_code_artifact"]
