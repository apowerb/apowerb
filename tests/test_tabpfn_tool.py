"""Tests for the TabPFN tool (#141).

The estimator is faked via _make_estimator, so no SDK call, no network and no
Prior Labs token are needed. These pin the tool's contract: validation, the
fit→predict flow, classification probabilities, and error mapping.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apowerb.tools_store.portfolio import tabpfn
from apowerb.tools_store.portfolio.tabpfn import tool_tabpfn_predict

_PATCH = "apowerb.tools_store.portfolio.tabpfn._make_estimator"

X_TRAIN = [[0.0, 1.0], [1.0, 0.0], [0.5, 0.5]]
Y_TRAIN = ["a", "b", "a"]
X_TEST = [[0.1, 0.9], [0.9, 0.1]]


def _fake_estimator(predict=None, proba=None, classes=None) -> MagicMock:
    est = MagicMock()
    est.fit.return_value = est
    if predict is not None:
        est.predict.return_value = predict
    if proba is not None:
        est.predict_proba.return_value = proba
    if classes is not None:
        est.classes_ = classes
    return est


# ── Validation (no estimator call) ─────────────────────────────────────────
class TestValidation:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("TABPFN_TOKEN", "tok")

    def test_bad_task(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                task="clustering",
                train_features=X_TRAIN,
                train_labels=Y_TRAIN,
                test_features=X_TEST,
            )
        assert result["status"] == "error"
        assert "task" in result["error_message"]
        mk.assert_not_called()

    def test_empty_train(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=[], train_labels=[], test_features=X_TEST
            )
        assert result["status"] == "error"
        mk.assert_not_called()

    def test_label_count_mismatch(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=X_TRAIN, train_labels=["a"], test_features=X_TEST
            )
        assert result["status"] == "error"
        assert "one label per" in result["error_message"]
        mk.assert_not_called()

    def test_ragged_rows(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=[[1, 2], [3]],
                train_labels=["a", "b"],
                test_features=X_TEST,
            )
        assert result["status"] == "error"
        assert "same length" in result["error_message"]
        mk.assert_not_called()

    def test_feature_count_mismatch_train_test(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=[[1, 2], [3, 4]],
                train_labels=["a", "b"],
                test_features=[[1, 2, 3]],
            )
        assert result["status"] == "error"
        assert "same number of features" in result["error_message"]
        mk.assert_not_called()

    def test_bad_output_type(self):
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=X_TRAIN,
                train_labels=Y_TRAIN,
                test_features=X_TEST,
                output_type="json",
            )
        assert result["status"] == "error"
        assert "output_type" in result["error_message"]
        mk.assert_not_called()

    def test_too_many_rows(self):
        big = [[0.0]] * 10_001
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=big, train_labels=[0] * 10_001, test_features=[[0.0]]
            )
        assert result["status"] == "error"
        assert "Too many rows" in result["error_message"]
        mk.assert_not_called()


# ── Missing token: error, no estimator ─────────────────────────────────────
class TestMissingToken:
    def test_no_token(self, monkeypatch):
        monkeypatch.delenv("TABPFN_TOKEN", raising=False)
        with patch(_PATCH) as mk:
            result = tool_tabpfn_predict(
                train_features=X_TRAIN, train_labels=Y_TRAIN, test_features=X_TEST
            )
        assert result["status"] == "error"
        assert "TABPFN_TOKEN" in result["error_message"]
        mk.assert_not_called()


# ── Happy path ─────────────────────────────────────────────────────────────
class TestPredict:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("TABPFN_TOKEN", "tok")

    def test_classification_labels(self):
        est = _fake_estimator(predict=["b", "a"])
        with patch(_PATCH, return_value=est) as mk:
            result = tool_tabpfn_predict(
                train_features=X_TRAIN, train_labels=Y_TRAIN, test_features=X_TEST
            )
        assert result["status"] == "success"
        assert result["task"] == "classification"
        assert result["predictions"] == ["b", "a"]
        assert result["n_train"] == 3
        assert result["n_test"] == 2
        mk.assert_called_once_with("classification", "tok")
        est.fit.assert_called_once()
        # fit got the training rows and labels
        fit_args = est.fit.call_args.args
        assert fit_args[0] == X_TRAIN
        assert fit_args[1] == Y_TRAIN

    def test_classification_probabilities(self):
        est = _fake_estimator(
            proba=_Arr([[0.2, 0.8], [0.7, 0.3]]), classes=_Arr(["a", "b"])
        )
        with patch(_PATCH, return_value=est):
            result = tool_tabpfn_predict(
                train_features=X_TRAIN,
                train_labels=Y_TRAIN,
                test_features=X_TEST,
                output_type="probabilities",
            )
        assert result["status"] == "success"
        assert result["classes"] == ["a", "b"]
        assert result["probabilities"] == [[0.2, 0.8], [0.7, 0.3]]
        est.predict_proba.assert_called_once()

    def test_regression(self):
        est = _fake_estimator(predict=_Arr([1.5, 2.5]))
        with patch(_PATCH, return_value=est) as mk:
            result = tool_tabpfn_predict(
                task="regression",
                train_features=X_TRAIN,
                train_labels=[1.0, 2.0, 3.0],
                test_features=X_TEST,
            )
        assert result["status"] == "success"
        assert result["task"] == "regression"
        assert result["predictions"] == [1.5, 2.5]
        mk.assert_called_once_with("regression", "tok")

    def test_sdk_error_mapped(self):
        with patch(_PATCH, side_effect=RuntimeError("401 invalid token")):
            result = tool_tabpfn_predict(
                train_features=X_TRAIN, train_labels=Y_TRAIN, test_features=X_TEST
            )
        assert result["status"] == "error"
        assert "Authentication failed" in result["error_message"]

    def test_generic_error_mapped(self):
        with patch(_PATCH, side_effect=RuntimeError("service unavailable")):
            result = tool_tabpfn_predict(
                train_features=X_TRAIN, train_labels=Y_TRAIN, test_features=X_TEST
            )
        assert result["status"] == "error"
        assert "TabPFN request failed" in result["error_message"]


# ── _to_list helper ────────────────────────────────────────────────────────
class TestToList:
    def test_numpy_like_converted(self):
        assert tabpfn._to_list(_Arr([1, 2, 3])) == [1, 2, 3]

    def test_plain_list_passthrough(self):
        assert tabpfn._to_list([1, 2]) == [1, 2]

    def test_none(self):
        assert tabpfn._to_list(None) is None


class _Arr:
    """Minimal numpy-like object exposing .tolist(), to avoid a numpy import."""

    def __init__(self, data):
        self._data = data

    def tolist(self):
        return self._data
