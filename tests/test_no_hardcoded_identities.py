"""No setting default, source file or test may carry someone's identity.

This is an open-source package published to a public repository. An identity
committed here is public forever, whether it sits in a configuration default,
a docstring example or a test fixture.

Three settings defaults named a real person until 0.1.8, and the loop it caused
(`owner user not found in DB`) was only noticed by running the published
Compose stack. The guard added then only walked ``Settings.model_fields``, so a
personal address landed in a docstring and a test on 2026-08-10 with a green
CI. These tests close that gap: the defaults are still checked, and the whole
source tree is checked too.

Two rules, deliberately different in strictness:

* A personal mailbox provider (gmail, outlook, yahoo, ...) is never legitimate
  here. No exemption list — use ``user@example.com``.
* A company or customer domain is often *functional* in this codebase: the SCEI
  tests exercise supplier filtering on real domains, and removing them would
  remove what they test. So that rule ratchets: the files that carry such an
  address today are frozen in ``_DOMAIN_DEBT``, and no new file may join them.
  Adding a file to that set is a conscious act, visible in review.

The model id ``thaink2/default`` is not an identity — it is the public name of
the shared-model provider, and renaming it would break every agent using it.
"""

import re
from pathlib import Path

from apowerb.configs.settings import Settings

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# The shared-model provider id, and only that.
ALLOWED = {"thaink2/default"}

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNED_ROOTS = ("src", "tests")

# Mailbox providers people use for their own address. Never legitimate in a
# public repository, in any form.
PERSONAL_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "outlook.fr",
    "yahoo.com",
    "yahoo.fr",
    "icloud.com",
    "protonmail.com",
    "proton.me",
    "live.com",
    "free.fr",
    "orange.fr",
    "wanadoo.fr",
    "sfr.fr",
    "laposte.net",
}

# Ours and our customers'. An address on one of these names a colleague, a
# client contact or an internal service account.
INTERNAL_DOMAINS = (
    "thaink2.com",
    "thaink2.fr",
    "th2ai.com",
    # The GCP service accounts of our own project.
    "th2ai.iam.gserviceaccount.com",
    "scei88.fr",
)

# Files that already carry an internal or customer address, frozen on
# 2026-08-10. This set may shrink, never grow — `rag.py` left it the same day,
# once its service account moved to the environment.
_DOMAIN_DEBT = {
    "tests/conftest.py",
    "tests/test_artifact_library.py",
    "tests/test_cli_agents.py",
    "tests/test_db_executor_mssql.py",
    "tests/test_notifier_health.py",
    "tests/test_run_gate_portes.py",
    "tests/test_scei_ar_assistant_template.py",
    "tests/test_scei_mail.py",
    "tests/test_scei_pmi_match.py",
    "tests/test_scei_schemas.py",
    "tests/test_scheduled_emailing.py",
    "tests/test_superagent_template_visibility.py",
    "tests/test_system_mailer.py",
    "tests/test_template_drift.py",
    "tests/test_webhook_gmail_signature.py",
    "tests/test_webhook_renewal.py",
    "tests/test_webhook_retrigger.py",
    "tests/test_webhook_serve_endpoints.py",
    "tests/test_webhook_user_id.py",
    "tests/unit_scei/test_ar_gate.py",
    "tests/unit_scei/test_excluded_scei88.py",
    "tests/unit_scei/test_pmi_gate_recovery.py",
    "tests/unit_scei/test_scei_mail_split_suppress.py",
}


def _string_defaults():
    for name, field in Settings.model_fields.items():
        default = field.default
        if isinstance(default, str) and default:
            yield name, default


def _python_files():
    for root in SCANNED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _addresses_in_tree():
    """(relative path, line number, address) for every address in the tree."""
    for path in _python_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            for match in EMAIL.finditer(line):
                yield relative, number, match.group(0)


def _domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower()


def test_no_email_address_in_a_default():
    offenders = {
        name: value for name, value in _string_defaults() if EMAIL.search(value)
    }
    assert not offenders, f"email address hardcoded as a default: {offenders}"


def test_no_company_domain_in_a_default():
    offenders = {
        name: value
        for name, value in _string_defaults()
        if "thaink2" in value and value not in ALLOWED
    }
    assert not offenders, f"company domain hardcoded as a default: {offenders}"


def test_no_personal_mailbox_anywhere_in_the_tree():
    offenders = [
        f"{path}:{number}: {address}"
        for path, number, address in _addresses_in_tree()
        if _domain_of(address) in PERSONAL_MAIL_DOMAINS
    ]
    assert not offenders, (
        "a personal mailbox address is committed to a public repository — "
        "use user@example.com instead:\n  " + "\n  ".join(sorted(offenders))
    )


def test_no_new_file_carries_an_internal_address():
    def is_internal(address: str) -> bool:
        domain = _domain_of(address)
        return any(
            domain == known or domain.endswith("." + known)
            for known in INTERNAL_DOMAINS
        )

    offenders = sorted(
        {
            f"{path}:{number}: {address}"
            for path, number, address in _addresses_in_tree()
            if is_internal(address) and path not in _DOMAIN_DEBT
        }
    )
    assert not offenders, (
        "a new file names a colleague, a customer contact or an internal "
        "service account. Use example.com, or add the file to _DOMAIN_DEBT "
        "and say in review why the real domain is needed:\n  "
        + "\n  ".join(offenders)
    )


def test_the_debt_list_stays_honest():
    """A path that no longer carries an address must leave the debt set.

    Without this, the set silently becomes a list of files nobody checks
    again, and the ratchet stops ratcheting.
    """
    carrying = {
        path
        for path, _, address in _addresses_in_tree()
        if any(
            _domain_of(address) == known or _domain_of(address).endswith("." + known)
            for known in INTERNAL_DOMAINS
        )
    }
    stale = sorted(_DOMAIN_DEBT - carrying)
    assert not stale, (
        "these files are listed as carrying an internal address but no longer "
        "do — remove them from _DOMAIN_DEBT:\n  " + "\n  ".join(stale)
    )
