"""Classification of package licenses against a license policy file.

The policy file is a JSON object mapping a category to the list of license
names in it, e.g. ``{"low_risk": [...], "copy_left": [...],
"non_commercial": [...]}``. Entries may be SPDX identifiers, free-form
license names or whole license expressions. The file itself is supplied by
the caller (``--license-compliance-file``); this tool never downloads or
bundles it.

Evaluation is deliberately conservative: a package is only considered
reviewable without OSRB involvement when *every* part of its license is in
the low-risk category. Unknown licenses and license names the policy does
not list always require review; nothing is assumed to be low risk.
"""

import json
import re

LOW_RISK = "low_risk"
UNKNOWN = "unknown"

# Categories from least to most restrictive. The policy file historically
# spells the non-commercial key "non_commerical"; both spellings are read.
CATEGORY_ORDER = [LOW_RISK, "copy_left", "non_commercial", UNKNOWN]
_KEY_ALIASES = {"non_commerical": "non_commercial"}

COMPLIANCE_FIELDS = [
    "license_category",
    "osrb_review_required",
    "license_unmatched",
]

# " | " joins licenses collected from several sources; OR/AND are SPDX
# operators. "/" is the legacy Cargo spelling of OR ("MIT/Apache-2.0"); it
# is split on only outside URLs, where it is part of the license name.
_SPLIT_RE = re.compile(r"\s+\|\s+|\s+(?:OR|AND)\s+", re.IGNORECASE)
_SLASH_RE = re.compile(r"\s*/\s*")
_WITH_RE = re.compile(r"\s+WITH\s+", re.IGNORECASE)


class LicensePolicyError(Exception):
    """The license policy file is unreadable or not in the expected shape."""


def _normalize(name):
    return " ".join(name.strip().strip("()").split()).lower()


class LicensePolicy:
    """Index of license name -> category built from a policy file."""

    def __init__(self, categories):
        """Build the index.

        Args:
            categories: Dict of category name -> list of license names

        Raises:
            LicensePolicyError: If a category is unknown or not a string list
        """
        self.index = {}
        self.conflicts = 0
        for raw_key, names in categories.items():
            key = _KEY_ALIASES.get(raw_key, raw_key)
            if key not in CATEGORY_ORDER or key == UNKNOWN:
                raise LicensePolicyError(f"unknown license category {raw_key!r}")
            if not isinstance(names, list) or not all(
                isinstance(n, str) for n in names
            ):
                raise LicensePolicyError(f"category {raw_key!r} is not a string list")
            for name in names:
                normalized = _normalize(name)
                if not normalized:
                    continue
                previous = self.index.get(normalized)
                if previous is not None and previous != key:
                    self.conflicts += 1
                    if _rank(previous) > _rank(key):
                        continue
                self.index[normalized] = key
        if not self.index:
            raise LicensePolicyError("no license names found")

    @classmethod
    def from_file(cls, path):
        """Load a policy file, failing fast on any problem."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise LicensePolicyError(f"cannot read {path}: {e}") from e
        if not isinstance(data, dict):
            raise LicensePolicyError(f"{path}: expected a JSON object of categories")
        return cls(data)

    def _lookup(self, part):
        """Category of one license part, trying it without a WITH exception."""
        normalized = _normalize(part)
        if normalized in self.index:
            return self.index[normalized]
        base = _WITH_RE.split(part, maxsplit=1)[0]
        if base != part:
            return self.index.get(_normalize(base))
        return None

    def evaluate(self, license_text):
        """Classify a package license string.

        Returns:
            Dict with license_category, osrb_review_required and
            license_unmatched (license parts the policy does not list)
        """
        text = (license_text or "").strip()
        if not text or text.lower() == "unknown":
            return _result(UNKNOWN, [])

        whole = self._lookup(text)
        if whole is not None:
            return _result(whole, [])

        categories = []
        unmatched = []
        for part in _parts(text):
            if not part or part.lower() == "unknown":
                if part:
                    categories.append(UNKNOWN)
                continue
            category = self._lookup(part)
            if category is None:
                unmatched.append(part)
                categories.append(UNKNOWN)
            else:
                categories.append(category)
        worst = max(categories, key=_rank) if categories else UNKNOWN
        return _result(worst, unmatched)


def _parts(text):
    """License parts of an expression, with legacy "/" alternatives split."""
    parts = []
    for part in _SPLIT_RE.split(text):
        part = part.strip().strip("()").strip()
        if "://" in part:
            parts.append(part)
        else:
            parts.extend(p.strip() for p in _SLASH_RE.split(part))
    return parts


def _rank(category):
    return CATEGORY_ORDER.index(category)


def _result(category, unmatched):
    return {
        "license_category": category,
        "osrb_review_required": category != LOW_RISK,
        "license_unmatched": unmatched,
    }
