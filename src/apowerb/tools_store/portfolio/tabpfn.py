"""TabPFN tool — tabular classification and regression via Prior Labs' API.

Trains a TabPFN model on a small tabular dataset and predicts labels (or class
probabilities / regression values) for new rows. TabPFN is a foundation model
for tabular data: no hyper-parameter tuning, a single fit+predict round.

Why the hosted API, not a local model
-------------------------------------
Inference runs on Prior Labs' hosted service through the ``tabpfn-client`` SDK,
which talks to ``https://api.priorlabs.ai`` and does **not** pull torch. The
heavy model never loads in this process, so the tool cannot OOM the app pod —
the reason a local TabPFN install was rejected.

API / SDK contract
------------------
Verified against docs.priorlabs.ai and github.com/PriorLabs/tabpfn-client
(2026-10-05): the SDK exposes a scikit-learn interface — ``TabPFNClassifier`` /
``TabPFNRegressor`` with ``.fit(X, y)``, ``.predict(X)`` and, for classifiers,
``.predict_proba(X)``. Auth is a bearer token set with
``tabpfn_client.set_access_token(<token>)`` (env var ``TABPFN_TOKEN``). The
service enforces row/feature/budget limits and bills some calls.

``TABPFN_TOKEN`` is read at module level so the ToolsStore parameter scanner
surfaces it as a UI setting; it is read again at call time.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Any

logger = getLogger(__name__)

# Read at module level so the ToolsStore scanner (regex on os.getenv) discovers
# it for the UI. Re-read at call time in _make_estimator.
_TABPFN_TOKEN = os.getenv("TABPFN_TOKEN", "")

_VALID_TASKS = ("classification", "regression")

# Guard rails before we send anything to the paid, rate-limited service. These
# are deliberately conservative; the live API may allow more, but a runaway
# caller should get a clear local error, not a surprise bill or a 429.
_MAX_TRAIN_ROWS = 10_000
_MAX_TEST_ROWS = 10_000
_MAX_FEATURES = 500


def _make_estimator(task: str, token: str) -> Any:
    """Authenticate and return a fresh TabPFN estimator for ``task``.

    Isolated so tests can patch it without importing the SDK. Lazy-imports
    ``tabpfn_client`` so the dependency is only touched when the tool runs.
    """
    import tabpfn_client  # type: ignore[import-untyped]

    tabpfn_client.set_access_token(token)
    if task == "classification":
        return tabpfn_client.TabPFNClassifier()
    return tabpfn_client.TabPFNRegressor()


def _validate_matrix(rows: object, name: str) -> list[list[Any]]:
    """Validate a 2-D feature matrix (non-empty, rectangular). Raise ValueError."""
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"`{name}` must be a non-empty list of rows.")
    if not all(isinstance(r, (list, tuple)) for r in rows):
        raise ValueError(f"`{name}` must be a list of rows (each row a list).")
    width = len(rows[0])
    if width == 0:
        raise ValueError(f"`{name}` rows must have at least one feature.")
    if any(len(r) != width for r in rows):
        raise ValueError(f"All rows in `{name}` must have the same length.")
    return [list(r) for r in rows]


def tool_tabpfn_predict(
    task: str = "classification",
    train_features: list[list[Any]] | None = None,
    train_labels: list[Any] | None = None,
    test_features: list[list[Any]] | None = None,
    output_type: str = "labels",
) -> dict:
    """Train TabPFN on a small table and predict for new rows.

    TabPFN fits and predicts in one shot — pass the labelled training rows and
    the rows to predict. Best for small/medium tabular data.

    Args:
        task (str): "classification" or "regression". Default: "classification".
        train_features (list[list]): Training rows, each a list of feature
            values (all rows the same length). Required.
        train_labels (list): The label for each training row — same length as
            ``train_features``. Required.
        test_features (list[list]): Rows to predict, same feature layout as the
            training rows. Required.
        output_type (str): For classification, "labels" (predicted class per
            row) or "probabilities" (per-class probabilities). Ignored for
            regression. Default: "labels".

    Returns:
        dict: On success, ``status`` "success" with ``predictions`` (one per test
        row), ``task``, ``n_train``/``n_test``, and, for classification
        probabilities, ``classes`` + ``probabilities``. On failure, ``status``
        "error" with ``error_message``.
    """
    if task not in _VALID_TASKS:
        return {
            "status": "error",
            "error_message": f"`task` must be one of {', '.join(_VALID_TASKS)}.",
        }
    if output_type not in ("labels", "probabilities"):
        return {
            "status": "error",
            "error_message": "`output_type` must be 'labels' or 'probabilities'.",
        }

    token = os.environ.get("TABPFN_TOKEN") or ""
    if not token:
        return {
            "status": "error",
            "error_message": "TABPFN_TOKEN not set. Configure your Prior Labs API token.",
        }

    try:
        x_train = _validate_matrix(train_features, "train_features")
        x_test = _validate_matrix(test_features, "test_features")
        if not isinstance(train_labels, list) or not train_labels:
            raise ValueError("`train_labels` must be a non-empty list.")
        if len(train_labels) != len(x_train):
            raise ValueError("`train_labels` must have one label per training row.")
        if len(x_train[0]) != len(x_test[0]):
            raise ValueError(
                "`test_features` rows must have the same number of features as "
                "`train_features`."
            )
    except ValueError as exc:
        return {"status": "error", "error_message": str(exc)}

    # Bound the request before hitting the paid service.
    n_features = len(x_train[0])
    if len(x_train) > _MAX_TRAIN_ROWS or len(x_test) > _MAX_TEST_ROWS:
        return {
            "status": "error",
            "error_message": (
                f"Too many rows (train {len(x_train)}, test {len(x_test)}). "
                f"Limits: {_MAX_TRAIN_ROWS} train, {_MAX_TEST_ROWS} test."
            ),
        }
    if n_features > _MAX_FEATURES:
        return {
            "status": "error",
            "error_message": f"Too many features ({n_features}). Limit: {_MAX_FEATURES}.",
        }

    try:
        estimator = _make_estimator(task, token)
        estimator.fit(x_train, train_labels)
        logger.info(
            "[TABPFN] %s fit on %d rows x %d features; predicting %d rows",
            task,
            len(x_train),
            n_features,
            len(x_test),
        )

        if task == "classification" and output_type == "probabilities":
            proba = estimator.predict_proba(x_test)
            classes = getattr(estimator, "classes_", None)
            return {
                "status": "success",
                "task": task,
                "n_train": len(x_train),
                "n_test": len(x_test),
                "classes": _to_list(classes),
                "probabilities": _to_list(proba),
            }

        predictions = estimator.predict(x_test)
        return {
            "status": "success",
            "task": task,
            "n_train": len(x_train),
            "n_test": len(x_test),
            "predictions": _to_list(predictions),
        }
    except Exception as exc:  # noqa: BLE001 — mapped to a structured error dict
        logger.warning("[TABPFN] prediction failed: %s", exc)
        msg = str(exc)
        if "token" in msg.lower() or "auth" in msg.lower() or "401" in msg:
            msg = f"Authentication failed (check TABPFN_TOKEN). {msg}"
        return {"status": "error", "error_message": f"TabPFN request failed: {msg}"}


def _to_list(value: object) -> Any:
    """Convert a numpy array / pandas object to plain Python for the JSON result."""
    if value is None:
        return None
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return tolist()
    return value
