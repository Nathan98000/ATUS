"""Documentation accuracy: the confirmed backend docs findings of the Phase 5
review, round 2.

Each test pins one finding (named in its docstring) so the docs cannot drift
back, and reads the source of truth next to the statement where there is one.
Read-only; needs neither Docker nor the network.
"""

from __future__ import annotations

from .conftest import REPO_ROOT
from .test_docs_accuracy import DOCS, ENV_PRODUCTION, _name, _read

CONFIG = REPO_ROOT / "src" / "atus_pipeline" / "config.py"


# --- verify-ca-accepted-without-hostname-check -----------------------------------------


def test_no_document_offers_verify_ca_as_an_alternative():
    """verify-ca-accepted-without-hostname-check: production accepts only verify-full,
    and every line of the docs that names verify-ca says why it is refused."""
    assert 'VERIFYING_SSLMODES = ("verify-full",)' in _read(CONFIG)
    named = 0
    for path in (*DOCS, ENV_PRODUCTION):
        for line in _read(path).splitlines():
            if "verify-ca" in line:
                named += 1
                assert "host name" in line, (_name(path), line[:160])
    assert named >= 4          # api.md, deployment.md, security.md R11, the template
