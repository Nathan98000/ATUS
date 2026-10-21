"""Documentation accuracy: the confirmed edge finding of the Phase 5 review, round 4.

cf-compromise-recovery-leaves-planted-state: the documented response to a leaked
Cloudflare token (rotate the secrets, bump ``EPOCH``) left behind what code deployed
with the token can write. KV entries planted under the next, public ``EPOCH``'s scope
were served by the very bump meant to purge them; governor Durable Object state was
never checked or reset; browsers kept that code's files under the names the recovery
build reuses. The code now tags KV entries and names the governor instance with
``IP_HMAC_KEY`` (``edge/test/worker/compromise.test.ts``) and salts the bundle names
(``frontend/src/deploy/assetNames.test.ts``); these tests keep the runbooks and the
threat model saying so, reading the code as the source of truth where there is one.

Read-only. Needs neither Docker nor the network.
"""

from __future__ import annotations

import re

from .conftest import REPO_ROOT
from .test_docs_accuracy import (
    ADR_011,
    ADR_015,
    DEPLOYMENT,
    DOCS_DIR,
    OPERATIONS,
    SECURITY,
    _flat,
    _read,
    _row,
    _section,
)
from .test_docs_accuracy_r2 import WRANGLER, _between

EDGE_SRC = REPO_ROOT / "edge" / "src"
ASSET_NAMES = REPO_ROOT / "frontend" / "src" / "deploy" / "assetNames.ts"
ENV_EXAMPLE = REPO_ROOT / ".env.production.example"


def _rotation_item_5() -> str:
    rotation = _flat(_section(_read(DEPLOYMENT), "### Rotation", "### TLS to the database"))
    return _between(rotation, "5. **Cloudflare API token**", "6. **Render deploy hook")


def test_the_code_tags_kv_entries_and_names_the_governor_with_ip_hmac_key():
    """The source of truth the docs below describe."""
    cache = _read(EDGE_SRC / "cache.ts")
    assert "export async function entryTag(" in cache
    assert "timingSafeEqualText(metadata.tag, expected)" in cache
    index = _read(EDGE_SRC / "index.ts")
    assert "const scope = env.IP_HMAC_KEY ? await cacheScope(env) : null" in index
    admission = _read(EDGE_SRC / "admission.ts")
    assert "export async function governorName(secret: string)" in admission
    assert "idFromName(await governorName(env.IP_HMAC_KEY))" in admission
    assert "GOVERNOR_NAME" not in _read(EDGE_SRC / "governor.ts")
    assert re.search(r"^export const ASSET_SALT = '[0-9a-f]{0,32}'$", _read(ASSET_NAMES), re.M)


def test_a_compromise_is_recovered_in_order_without_an_epoch_bump():
    """Your code first, from a new commit with a fresh ASSET_SALT (never a rollback);
    then every Worker secret, IP_HMAC_KEY first; never back across the rotation; and
    no reliance on EPOCH, whose next value is public."""
    item = _rotation_item_5()
    assert "bump EPOCH in edge/wrangler.jsonc and deploy" not in item
    assert "Do not rely on an EPOCH bump" in item
    order = ["wrangler deployments list", "ASSET_SALT", "frontend/src/deploy/assetNames.ts",
             "openssl rand -hex 8", "Not wrangler rollback",
             "rotate every Worker secret: IP_HMAC_KEY first", "put RENDER_ORIGIN again",
             "Never roll back to a version from before step 3"]
    positions = [item.index(needle) for needle in order]
    assert positions == sorted(positions), dict(zip(order, positions, strict=True))
    for needle in ("no entry that version wrote verifies", "fresh instance", "back to back",
                   "new SNAPSHOT_ID", "security.md R16",
                   # Such a version may delete a required secret, so that wrangler refuses
                   # the deploy; a secret put then deploys that version's code with the value.
                   "a throwaway value for it", "never put a value you keep"):
        assert needle in item, needle
    # The salt renames the bundles only: the page still names /boot.css and /favicon.svg
    # (index.html), which such a version may have sent as immutable too.
    assert "never asks for a file whose name" not in item
    assert "(the page, /boot.css, /favicon.svg)" in item


def test_the_other_runbooks_and_the_threat_model_say_the_same():
    cloudflare = _flat(next(line for line in _read(OPERATIONS).splitlines()
                            if line.startswith("| Cloudflare API token (90-day TTL)")))
    assert "bump EPOCH" not in cloudflare and "Do not rely on an EPOCH bump" in cloudflare
    assert cloudflare.index("ASSET_SALT") < cloudflare.index("IP_HMAC_KEY first")
    assert "never with wrangler rollback" in cloudflare

    security = _read(SECURITY)
    token = _flat(_row(security, "`CLOUDFLARE_API_TOKEN`"))
    assert "bump EPOCH (" not in token
    for needle in ("ASSET_SALT", "IP_HMAC_KEY first", "invalidates every KV entry (C10)",
                   "fresh governor instance (C6)", "R16"):
        assert needle in token, needle
    c10 = _flat(_section(security, "**C10. KV store gate.**", "Code:"))
    assert "**Tag.**" in c10 and "IP_HMAC_KEY" in c10 and "does not verify" in c10
    c6 = _flat(_section(security, "**C6. Quota governor**", "Code:"))
    assert "named by an HMAC of IP_HMAC_KEY" in c6 and "Neon ground truth" in c6
    assert "(one SQLite Durable Object, global)" not in c6
    # Without the key no tag can be checked, so KV hits stop too (compromise.test.ts).
    assert "a missing IP_HMAC_KEY, except that KV hits stop too" in c6
    api = _flat(_row(_read(DOCS_DIR / "api.md"), "503 | `capacity_unavailable`"))
    assert "the edge cache is off too" in api
    frontend = _flat(_read(DOCS_DIR / "frontend.md"))
    assert "ASSET_SALT in src/deploy/assetNames.ts" in frontend
    risks = _section(security, "## Accepted risks", "\n## ")
    numbers = [int(n) for n in re.findall(r"^\*\*R(\d+)\.", risks, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    r16 = _flat(risks[risks.index("**R16."):])
    assert "ASSET_SALT" in r16 and "SNAPSHOT_ID" in r16 and "service worker" in r16
    assert "never asks for a name such a version could have filled" not in r16
    assert "/boot.css and /favicon.svg" in r16 and "cache: 'no-cache'" in r16
    index_html = _read(REPO_ROOT / "frontend" / "index.html")
    unhashed = set(re.findall(r'(?:href|src)="(/[^"/]+\.(?:css|svg|js))"', index_html))
    assert unhashed == {"/boot.css", "/favicon.svg"}, unhashed

    adr = _read(ADR_015)
    l1 = _flat(next(line for line in adr.splitlines() if line.startswith("- **L1:**")))
    assert "bump EPOCH." not in l1 and "IP_HMAC_KEY first" in l1
    governor = _flat(_section(adr, "### Quota governor", "**Who is charged.**"))
    assert "named global." not in governor and "IP_HMAC_KEY" in governor
    assert "## Amendments (review round 4" in adr
    assert "named `global`," not in _read(ADR_011)


def test_every_ip_hmac_key_row_says_what_a_rotation_restarts():
    """A rotation now starts a fresh governor instance (the day's counts restart) and
    orphans every KV entry: each place that tells the owner how to rotate it says so."""
    deployment = _read(DEPLOYMENT)
    row = _flat(_row(deployment, "`IP_HMAC_KEY`"))
    assert "fresh governor instance" in row and "orphans every KV entry" in row
    rotation = _flat(_section(deployment, "### Rotation", "### TLS to the database"))
    item = _between(rotation, "4. **IP_HMAC_KEY.**", "5. **Cloudflare API token**")
    assert "fresh governor instance" in item and "every KV entry is orphaned" in item
    assert "00:00 UTC" in item
    epoch = _flat(_row(deployment, "`EPOCH`"))
    assert "Not the purge after a Cloudflare compromise" in epoch
    ops = _flat(_row(_read(OPERATIONS), "`IP_HMAC_KEY`"))
    assert "fresh governor instance" in ops and "every KV entry is orphaned" in ops
    wrangler = _flat(_read(WRANGLER))
    assert "names the governor's instance and tags every KV entry" in wrangler
    assert 'named "global"' not in wrangler
    assert "names the governor's instance and tags every KV entry" in _flat(_read(ENV_EXAMPLE))
