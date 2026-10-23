"""Harness counter-example: every line breaks a rule. Excluded from pre-commit, never imported."""
import os
import pdb
from datetime import datetime

from apowerb.routers import agents
from th2agent_billing import charge

API_KEY = os.getenv("API_KEY")


def broken():  # type: ignore
    print(datetime.utcnow(), agents, charge, pdb)
    try:
        return 1
    except:
        return 0
