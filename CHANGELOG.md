# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed

- **Breaking:** `MageAPIClient` and `Th2etlAPIClient` no longer return
  `[]`, `None`, or `False` on orchestrator failure. All 28 methods across
  both classes now raise `OrchestratorUnavailable` instead. Two methods
  also changed their annotated return type from `dict[str, Any]` to
  `dict[str, Any] | None`.

  Any code importing these clients directly (rather than going through
  the routers) must catch `OrchestratorUnavailable` — see
  `apowerb/scheduler/th2etl_client.py` for the exception's `unreachable`
  and `status` attributes, which distinguish "orchestrator unreachable"
  from "orchestrator answered with an error".