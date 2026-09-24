"""Unit tests for tools_store.portfolio.jev — the Jev decision tools.

The client is replaced by a fake that answers from the item text, so the
tests check what the tools promise: one decision per item, uncertain items
flagged instead of forced into a label, counts that add up, and errors
returned as data rather than raised.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apowerb.integrations.jev_client import (
    JevAPIError,
    JevNotConfigured,
    JevUnavailable,
)
from apowerb.tools_store.portfolio import jev

_LABELS = {
    "bug": "A defect in the product",
    "question": "A usage question",
    "feature": "A feature request",
}


class _FakeClient:
    """Answers ``choice`` questions from keywords, ``noul`` from a table."""

    def __init__(self, noul=None):
        self.calls = []
        self.noul = noul or {}

    def decide(self, state, questions):
        self.calls.append((state, questions))
        text = str(state.get("item", ""))
        answers = {}
        for qid, q in questions.items():
            if q["type"] == "choice":
                if "crash" in text:
                    probs = {"bug": 0.92, "question": 0.05, "feature": 0.03}
                elif "how" in text:
                    probs = {"bug": 0.1, "question": 0.85, "feature": 0.05}
                else:
                    probs = {"bug": 0.4, "question": 0.3, "feature": 0.3}
                best = max(probs, key=probs.get)
                answers[qid] = {
                    "type": "choice",
                    "choice": best,
                    "confidence": probs[best],
                    "probabilities": probs,
                }
            else:
                answers[qid] = {"noul": self.noul.get(text, 0.5)}
        return {"answers": answers, "usage": {"cost": 0.0001}}


@pytest.fixture
def fake():
    client = _FakeClient(noul={"big deal": 0.95, "small deal": 0.2, "mid deal": 0.6})
    with patch.object(jev, "JevClient", return_value=client):
        yield client


class TestClassify:
    def test_labels_each_item_and_flags_uncertain(self, fake):
        result = jev.tool_jev_classify(
            labels=_LABELS,
            items=["app crash on save", "how do I export?", "something vague"],
        )
        assert result["status"] == "success"
        assert [r["label"] for r in result["results"]] == ["bug", "question", None]
        assert [r["uncertain"] for r in result["results"]] == [False, False, True]
        assert result["results"][2]["best_guess"] == "bug"
        assert result["counts"] == {
            "bug": 1,
            "question": 1,
            "feature": 0,
            "uncertain": 1,
        }
        assert result["total"] == 3

    def test_question_is_a_choice_over_the_given_labels(self, fake):
        jev.tool_jev_classify(
            labels=_LABELS, items=["app crash"], instructions="Support tickets"
        )
        state, questions = fake.calls[0]
        (question,) = questions.values()
        assert question["type"] == "choice"
        assert question["criteria"] == _LABELS
        assert "Support tickets" in question["instructions"]
        assert state["item"] == "app crash"

    def test_min_confidence_is_respected(self, fake):
        result = jev.tool_jev_classify(
            labels=_LABELS, items=["how do I export?"], min_confidence=0.9
        )
        assert result["results"][0]["uncertain"] is True

    def test_rows_from_sql_use_the_text_column(self, fake):
        rows = [{"id": 1, "body": "crash at login"}, {"id": 2, "body": "how to share?"}]
        with patch.object(
            jev, "tool_run_sql", return_value={"success": True, "data": rows}
        ):
            result = jev.tool_jev_classify(
                labels=_LABELS, sql="SELECT id, body FROM tickets", text_column="body"
            )
        assert [r["label"] for r in result["results"]] == ["bug", "question"]
        assert result["results"][0]["row"] == {"id": 1, "body": "crash at login"}

    def test_missing_text_column_is_an_error(self, fake):
        with patch.object(
            jev, "tool_run_sql", return_value={"success": True, "data": [{"id": 1}]}
        ):
            result = jev.tool_jev_classify(
                labels=_LABELS, sql="SELECT id FROM t", text_column="body"
            )
        assert result["status"] == "error"
        assert "body" in result["message"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"labels": {"only": "one"}, "items": ["x"]},
            {"labels": _LABELS},
            {"labels": _LABELS, "items": ["x"], "sql": "SELECT 1"},
            {"labels": _LABELS, "items": []},
        ],
    )
    def test_invalid_arguments(self, fake, kwargs):
        result = jev.tool_jev_classify(**kwargs)
        assert result["status"] == "error"
        assert fake.calls == []

    def test_item_cap(self, fake):
        with patch.object(jev, "_max_items", return_value=2):
            result = jev.tool_jev_classify(labels=_LABELS, items=["a", "b", "c"])
        assert result["status"] == "error"
        assert "2" in result["message"]
        assert fake.calls == []

    def test_results_are_truncated_but_counts_are_complete(self, fake):
        items = ["app crash"] * (jev._MAX_RETURNED_RESULTS + 5)
        result = jev.tool_jev_classify(labels=_LABELS, items=items)
        assert len(result["results"]) == jev._MAX_RETURNED_RESULTS
        assert result["counts"]["bug"] == len(items)
        assert result["truncated"] is True

    def test_per_item_failure_is_reported_not_raised(self):
        client = MagicMock()
        client.decide.side_effect = [
            {
                "answers": {
                    "label": {
                        "type": "choice",
                        "choice": "bug",
                        "confidence": 0.9,
                        "probabilities": {
                            "bug": 0.9,
                            "question": 0.05,
                            "feature": 0.05,
                        },
                    }
                }
            },
            JevUnavailable("timeout"),
        ]
        with (
            patch.object(jev, "JevClient", return_value=client),
            patch.object(jev, "_WORKERS", 1),
        ):
            result = jev.tool_jev_classify(labels=_LABELS, items=["crash", "other"])
        assert result["status"] == "success"
        assert result["counts"]["failed"] == 1
        assert result["results"][1]["error"]

    def test_malformed_answer_fails_the_item_not_the_call(self):
        client = MagicMock()
        client.decide.side_effect = [
            {"answers": {"WRONG_KEY": {"choice": "bug"}}},
            {"answers": {"label": {"choice": "bug", "confidence": 0.95}}},
        ]
        with (
            patch.object(jev, "JevClient", return_value=client),
            patch.object(jev, "_WORKERS", 1),
        ):
            result = jev.tool_jev_classify(labels=_LABELS, items=["a", "b"])
        assert result["status"] == "success"
        assert result["counts"]["failed"] == 1
        assert result["counts"]["bug"] == 1

    def test_null_text_is_skipped_not_sent_as_none(self, fake):
        rows = [{"id": 1, "body": None}, {"id": 2, "body": "crash at login"}]
        with patch.object(
            jev, "tool_run_sql", return_value={"success": True, "data": rows}
        ):
            result = jev.tool_jev_classify(
                labels=_LABELS, sql="SELECT id, body FROM tickets", text_column="body"
            )
        assert [state["item"] for state, _ in fake.calls] == ["crash at login"]
        assert result["total"] == 1
        assert result["skipped_empty"] == 1


class TestDecide:
    def test_returns_probability_and_verdict(self, fake):
        result = jev.tool_jev_decide(
            question="Is this deal worth pursuing?", context="big deal"
        )
        assert result == {"status": "success", "probability": 0.95, "decision": "yes"}

    @pytest.mark.parametrize(
        "text,verdict", [("small deal", "no"), ("mid deal", "uncertain")]
    )
    def test_uncertain_band(self, fake, text, verdict):
        assert (
            jev.tool_jev_decide(question="Worth it?", context=text)["decision"]
            == verdict
        )

    def test_criteria_are_forwarded(self, fake):
        jev.tool_jev_decide(
            question="Q?", context="big deal", yes_means="Y", no_means="N"
        )
        _, questions = fake.calls[0]
        assert questions["decision"]["criteria"] == {"true": "Y", "false": "N"}

    def test_empty_question_is_an_error(self, fake):
        assert jev.tool_jev_decide(question=" ", context="x")["status"] == "error"


class TestScore:
    def test_ranks_items_by_probability(self, fake):
        result = jev.tool_jev_score(
            criterion="High value deal",
            items=["small deal", "big deal", "mid deal"],
            top_k=2,
        )
        assert result["status"] == "success"
        assert [r["item"] for r in result["ranking"]] == ["big deal", "mid deal"]
        assert result["ranking"][0]["probability"] == 0.95
        assert result["total"] == 3
        assert result["above_threshold"] == 1

    def test_malformed_answer_fails_the_item_not_the_call(self):
        client = MagicMock()
        client.decide.side_effect = [
            {"answers": {"decision": {"noul": 0.9}}},
            {"answers": {}},
        ]
        with (
            patch.object(jev, "JevClient", return_value=client),
            patch.object(jev, "_WORKERS", 1),
        ):
            result = jev.tool_jev_score(criterion="Urgent", items=["a", "b"])
        assert result["status"] == "success"
        assert result["failed"] == 1
        assert [r["item"] for r in result["ranking"]] == ["a"]


class TestErrors:
    def test_not_configured_is_a_clear_error(self):
        with patch.object(
            jev, "JevClient", side_effect=JevNotConfigured("JEV_API_KEY")
        ):
            result = jev.tool_jev_decide(question="Q?", context="x")
        assert result["status"] == "error"
        assert "JEV_API_KEY" in result["message"]

    def test_api_error_uses_the_user_message(self):
        client = MagicMock()
        client.decide.side_effect = JevAPIError(402, "Crédit Jev épuisé.")
        with patch.object(jev, "JevClient", return_value=client):
            result = jev.tool_jev_decide(question="Q?", context="x")
        assert result == {"status": "error", "message": "Crédit Jev épuisé."}
