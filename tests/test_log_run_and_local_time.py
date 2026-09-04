"""Deux défauts de lisibilité constatés en réunion (04/09/2026).

1. Un flux de logs Docker concatène les redémarrages : les erreurs d un
   ancien run se lisent comme celles du nouveau. Chaque ligne porte
   désormais un ``run_id``, et chaque démarrage ouvre par une bannière.
2. Les horodatages sortaient en UTC alors que l équipe raisonne en heure
   de Paris. Le format texte affiche le fuseau demandé, offset compris ;
   le JSON garde son ``timestamp`` UTC (contrat des pipelines) et gagne
   un ``timestamp_local``.
"""

from __future__ import annotations

import io
import json
import logging
import re

import pytest

from apowerb.configs import th2logger


@pytest.fixture(autouse=True)
def _reset_logging(monkeypatch):
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for var in ("TH2_LOG_TZ", "TZ", "TH2_LOG_RUN_ID", "TH2_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(th2logger, "_RUN_ID", None, raising=False)
    monkeypatch.setattr(th2logger, "_BANNERED_RUN", None, raising=False)
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


def _emit(stream, **kwargs):
    th2logger.configure_structured_logging(level=logging.INFO, stream=stream, **kwargs)
    logging.getLogger("test.runs").info("une ligne")
    return stream.getvalue()


class TestRunSeparation:
    def test_text_lines_carry_the_run_id(self):
        out = _emit(io.StringIO(), fmt="text")
        assert th2logger.current_run_id() in out.splitlines()[-1]

    def test_json_records_carry_the_run_id(self):
        out = _emit(io.StringIO(), fmt="json")
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["run_id"] == th2logger.current_run_id()

    def test_a_banner_opens_every_run(self):
        out = _emit(io.StringIO(), fmt="text")
        first = out.splitlines()[0]
        assert th2logger.current_run_id() in first
        assert "run started" in first

    def test_the_banner_stays_parseable_in_json_mode(self):
        out = _emit(io.StringIO(), fmt="json")
        first = json.loads(out.strip().splitlines()[0])
        assert first["run_id"] == th2logger.current_run_id()
        assert "run started" in first["message"]

    def test_two_runs_do_not_share_an_id(self, monkeypatch):
        # One id per process: each run below stands for a separate boot.
        monkeypatch.setenv("TH2_LOG_RUN_ID", "deploy-a")
        first = _emit(io.StringIO(), fmt="text")
        monkeypatch.setattr(th2logger, "_RUN_ID", None)
        monkeypatch.setattr(th2logger, "_BANNERED_RUN", None)
        monkeypatch.setenv("TH2_LOG_RUN_ID", "deploy-b")
        second = _emit(io.StringIO(), fmt="text")
        assert "deploy-a" in first and "deploy-b" not in first
        assert "deploy-b" in second and "deploy-a" not in second

    def test_an_operator_supplied_id_prefixes_the_run(self, monkeypatch):
        monkeypatch.setenv("TH2_LOG_RUN_ID", "release-0.2.9")
        out = _emit(io.StringIO(), fmt="text")
        assert "release-0.2.9." in out

    def test_two_starts_of_one_deployment_still_differ(self, monkeypatch):
        """The defect this field exists to fix.

        A restart of the same deployment must not read as the same run --
        that is exactly the case where an old error looks fresh.
        """
        monkeypatch.setenv("TH2_LOG_RUN_ID", "release-0.2.9")
        first = th2logger.current_run_id()
        monkeypatch.setattr(th2logger, "_RUN_ID", None)  # a second process
        second = th2logger.current_run_id()
        assert first != second
        assert first.startswith("release-0.2.9.")
        assert second.startswith("release-0.2.9.")

    def test_a_second_configuration_of_the_same_run_adds_no_banner(self):
        # Startup configures root more than once; two banners in one boot
        # would separate nothing.
        stream = io.StringIO()
        _emit(stream, fmt="text")
        _emit(stream, fmt="text")
        banners = [
            line for line in stream.getvalue().splitlines() if "run started" in line
        ]
        assert len(banners) == 1


class TestLocalTime:
    def test_text_timestamps_follow_the_configured_zone(self, monkeypatch):
        monkeypatch.setenv("TH2_LOG_TZ", "Europe/Paris")
        out = _emit(io.StringIO(), fmt="text")
        # Paris is +01:00 or +02:00 depending on the season; never +00:00.
        assert re.search(r"\+0[12]:00", out.splitlines()[-1])

    def test_utc_stays_the_default_when_nothing_is_configured(self):
        out = _emit(io.StringIO(), fmt="text")
        assert "+00:00" in out.splitlines()[-1]

    def test_json_keeps_utc_and_adds_the_local_reading(self, monkeypatch):
        monkeypatch.setenv("TH2_LOG_TZ", "Europe/Paris")
        payload = json.loads(_emit(io.StringIO(), fmt="json").strip().splitlines()[-1])
        assert payload["timestamp"].endswith("+00:00")
        assert re.search(r"\+0[12]:00$", payload["timestamp_local"])

    def test_the_plain_tz_variable_is_honoured_when_th2_log_tz_is_absent(
        self, monkeypatch
    ):
        monkeypatch.setenv("TZ", "Europe/Paris")
        out = _emit(io.StringIO(), fmt="text")
        assert re.search(r"\+0[12]:00", out.splitlines()[-1])

    def test_an_unknown_zone_falls_back_to_utc_instead_of_crashing(self, monkeypatch):
        monkeypatch.setenv("TH2_LOG_TZ", "Mars/Olympus_Mons")
        out = _emit(io.StringIO(), fmt="text")
        assert "+00:00" in out.splitlines()[-1]
