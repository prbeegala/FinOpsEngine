"""tag_keys.py — Shared resource-tag conventions across FinOps Engine tools.

Single source of truth for the case-insensitive tag-key tuples that
``context-enricher`` (and now ``hidden-waste``) consult when reading
``owner`` / ``criticality`` / ``environment`` / ``cost-centre`` /
``application`` from Azure Resource tags.

Keep these tuples ordered most-specific → most-generic; the per-tool
helpers iterate in declared order and stop at the first non-empty,
non-placeholder match.

No third-party dependencies — uses the Python standard library only.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Tag-key conventions (case-insensitive lookup, first match wins)
# ---------------------------------------------------------------------------

OWNER_KEYS: tuple[str, ...] = (
    "owned by", "managed by", "owner", "team", "domain",
    "approval group", "support group", "department",
    "responsibleteam", "ops_owner",
)

CRITICALITY_KEYS: tuple[str, ...] = (
    "business criticality", "criticality", "service tier",
    "businesscriticality", "tier", "businesstier",
)

ENVIRONMENT_KEYS: tuple[str, ...] = ("environment", "env")

COSTCENTRE_KEYS: tuple[str, ...] = (
    "cost centre", "cost center", "costcenter", "costcentre",
    "cost_centre", "cost_center",
)

APP_KEYS: tuple[str, ...] = (
    "service", "product", "application", "app", "appname",
    "applicationname", "project",
)

# ---------------------------------------------------------------------------
# Tag-value conventions
# ---------------------------------------------------------------------------

# Common placeholder values that should be treated as "no value".
TAG_PLACEHOLDERS: frozenset[str] = frozenset({
    "untagged", "n/a", "na", "tbc", "tbd", "none", "-", "",
})

# Environment-tag values that signal a non-production workload — the
# population of resources eligible for dev/test cost optimisations
# (auto-shutdown, serverless tiers, smaller SKUs). Lower-cased here for
# case-insensitive matching at the call site.
DEVTEST_ENV_VALUES: frozenset[str] = frozenset({
    "dev", "develop", "development",
    "test", "testing",
    "qa", "quality",
    "uat", "useracceptance", "user-acceptance",
    "stg", "stage", "staging",
    "preprod", "pre-prod", "pre-production", "preproduction",
    "sandbox", "sbx", "sand",
    "devtest", "dev-test", "dev/test",
    "nonprod", "non-prod", "non-production", "nonproduction",
})


def is_devtest(value: str | None) -> bool:
    """Return True if an environment-tag value designates a non-prod workload.

    Matching is case-insensitive and ignores surrounding whitespace; an
    empty / placeholder value returns False.
    """
    if not value:
        return False
    v = value.strip().lower()
    if not v or v in TAG_PLACEHOLDERS:
        return False
    return v in DEVTEST_ENV_VALUES
