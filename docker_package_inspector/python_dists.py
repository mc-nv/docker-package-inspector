"""Discovery of installed Python distributions from their metadata directories.

Instead of asking an installer (pip) what it installed, the container
filesystem is walked for ``*.dist-info`` / ``*.egg-info`` entries. This finds
packages installed by any tool (pip, uv, conda, distro packages) in any
environment (system site-packages, virtualenvs, tool directories), and works
in images that have no pip or no python at all, since only ``/bin/sh`` and
coreutils/busybox are required.
"""

import re
from email.parser import HeaderParser

# Paths whose Python environments are not the image's main interpreter:
# vendored copies inside another package, installer caches (uv unpacks wheels
# with their dist-info into its cache), tool-private interpreters (Nsight
# Systems ships its own Python) and backend-private wheel directories. These
# are still reported, only flagged, so nothing is silently dropped.
PRIVATE_ENV_PATTERNS = [
    re.compile(r"/_vendor/"),
    re.compile(r"/\.cache/"),
    re.compile(r"/nsight[^/]*/", re.IGNORECASE),
    re.compile(r"^/opt/tritonserver/backends/[^/]+/"),
]

_LICENSE_NAME_RE = r"(licen[cs]e|copying|notice|copyright|authors)[^/]*"

# Emits one block per distribution:
#   @@DIST <path>
#   @@INSTALLER <first line of INSTALLER>
#   @@METADATA
#   <METADATA/PKG-INFO header block>
#   @@LICENSE_FILE <absolute path>      (verified non-empty, repeated)
#   @@COPYRIGHT <candidate line>        (repeated)
#   @@END
DIST_SCAN_SCRIPT = r"""
find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o \
  \( -name '*.dist-info' -o -name '*.egg-info' \) -print 2>/dev/null |
while IFS= read -r d; do
  printf '@@DIST %s\n' "$d"
  if [ -f "$d" ]; then
    m="$d"
    printf '@@INSTALLER \n'
  else
    m="$d/METADATA"
    [ -f "$m" ] || m="$d/PKG-INFO"
    printf '@@INSTALLER %s\n' "$(head -n 1 "$d/INSTALLER" 2>/dev/null)"
  fi
  printf '@@METADATA\n'
  sed '/^$/q' "$m" 2>/dev/null | head -c 65536
  printf '\n'
  if [ -d "$d" ]; then
    base=$(dirname "$d")
    {
      find "$d" -type f \( -path "$d/licenses/*" \
        -o -iname 'licen[cs]e*' -o -iname 'copying*' -o -iname 'notice*' \
        -o -iname 'copyright*' -o -iname 'authors*' \) 2>/dev/null
      if [ -f "$d/RECORD" ]; then
        cut -d, -f1 "$d/RECORD" |
          grep -iE '(^|/)LICENSE_NAME_RE$' |
          grep -viE '\.(py|pyc|pyi|so|js|html)$' |
          while IFS= read -r f; do printf '%s/%s\n' "$base" "$f"; done
      fi
    } | sort -u | while IFS= read -r f; do
      if [ -s "$f" ]; then
        printf '@@LICENSE_FILE %s\n' "$f"
        grep -iE 'copyright|\(c\)' "$f" 2>/dev/null | head -n 20 |
          sed 's/^/@@COPYRIGHT /'
      fi
    done
  fi
  printf '@@END\n'
done
""".replace(
    "LICENSE_NAME_RE", _LICENSE_NAME_RE
)

_COPYRIGHT_RE = re.compile(
    r"^\s*(copyright\b.*(\d{4}|\(c\)|©)|(\(c\)|©)\s*\d{4}.*)", re.IGNORECASE
)
_COPYRIGHT_PLACEHOLDER_RE = re.compile(
    r"\[yyyy\]|\{yyyy\}|<year>|name of copyright owner|\[name of", re.IGNORECASE
)
_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_name(name):
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_private_env(path):
    """Return True if a distribution path belongs to a non-main environment."""
    return any(pattern.search(path) for pattern in PRIVATE_ENV_PATTERNS)


def extract_copyright_lines(lines):
    """Keep only lines that state a copyright holder, without placeholders.

    Args:
        lines: Iterable of candidate text lines

    Returns:
        De-duplicated list of stripped copyright lines, in input order
    """
    found = []
    for line in lines:
        line = " ".join(line.split())
        if not _COPYRIGHT_RE.match(line) or _COPYRIGHT_PLACEHOLDER_RE.search(line):
            continue
        if line not in found:
            found.append(line)
    return found


def parse_dist_scan(output):
    """Parse DIST_SCAN_SCRIPT output into one dict per distribution path.

    Returns:
        List of dicts with keys: path, installer, metadata (email.message),
        license_files, copyright
    """
    dists = []
    current = None
    in_metadata = False
    metadata_lines = []
    copyright_lines = []

    for line in output.splitlines():
        if line.startswith("@@DIST "):
            current = {
                "path": line[len("@@DIST ") :],
                "installer": "",
                "license_files": [],
            }
            in_metadata = False
            metadata_lines = []
            copyright_lines = []
        elif current is None:
            continue
        elif line.startswith("@@INSTALLER"):
            current["installer"] = line[len("@@INSTALLER") :].strip()
        elif line == "@@METADATA":
            in_metadata = True
        elif line.startswith("@@LICENSE_FILE "):
            in_metadata = False
            current["license_files"].append(line[len("@@LICENSE_FILE ") :])
        elif line.startswith("@@COPYRIGHT "):
            in_metadata = False
            copyright_lines.append(line[len("@@COPYRIGHT ") :])
        elif line == "@@END":
            current["metadata"] = HeaderParser().parsestr("\n".join(metadata_lines))
            current["copyright"] = extract_copyright_lines(copyright_lines)
            if current["metadata"].get("Name"):
                dists.append(current)
            current = None
            in_metadata = False
        elif in_metadata:
            metadata_lines.append(line)

    return dists


def installer_to_provider(installer):
    """Map an INSTALLER value to the package_provider reported in output."""
    installer = installer.strip()
    if not installer:
        return "Unknown"
    if installer.lower() == "pip":
        return "PIP"
    return installer


def runtime_requirements(metadata):
    """Names of Requires-Dist entries that are not gated behind an extra."""
    names = []
    for requirement in metadata.get_all("Requires-Dist") or []:
        if "extra" in requirement.partition(";")[2]:
            continue
        match = _REQUIREMENT_NAME_RE.match(requirement)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def group_dists(dists):
    """Group per-path distributions into one entry per normalized project name.

    The primary entry (used for license metadata) is the first one found in a
    main environment, falling back to the first private one. Version and
    provider describe the main-environment copies when there are any; paths,
    license files and copyright lines cover every copy.

    Returns:
        List of dicts with keys: name, version, provider, primary, paths,
        license_files, copyright, is_private_env, requires
    """
    groups = {}
    for dist in dists:
        key = normalize_name(dist["metadata"]["Name"])
        groups.setdefault(key, []).append(dist)

    grouped = []
    for members in groups.values():
        private = [is_private_env(d["path"]) for d in members]
        primary = members[private.index(False)] if False in private else members[0]
        described = [d for d, p in zip(members, private) if not p] or members

        versions = sorted({d["metadata"].get("Version", "") for d in described})
        providers = sorted({installer_to_provider(d["installer"]) for d in described})

        license_files = []
        copyright_lines = []
        requires = []
        for dist in members:
            license_files.extend(
                f for f in dist["license_files"] if f not in license_files
            )
            copyright_lines.extend(
                c for c in dist["copyright"] if c not in copyright_lines
            )
            requires.extend(
                r for r in runtime_requirements(dist["metadata"]) if r not in requires
            )

        grouped.append(
            {
                "name": primary["metadata"]["Name"],
                "version": ", ".join(versions),
                "provider": ", ".join(providers),
                "primary": primary,
                "paths": [d["path"] for d in members],
                "license_files": license_files,
                "copyright": copyright_lines,
                "is_private_env": all(private),
                "requires": requires,
            }
        )

    return grouped


def local_license(metadata):
    """License declared in METADATA, as (license, source) or (None, None).

    License-Expression (PEP 639) is authoritative; the free-form License field
    and License classifiers are returned only as a fallback by the caller.
    """
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression, "METADATA License-Expression"
    return None, None


def local_license_fallback(metadata):
    """License from the free-form License field or classifiers, or None."""
    licenses = []
    field = (metadata.get("License") or "").strip()
    if field and field.upper() != "UNKNOWN":
        licenses.append(" ".join(field.split()))
    for classifier in metadata.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            name = classifier.split("::")[-1].strip()
            if name not in licenses:
                licenses.append(name)
    return " | ".join(licenses) if licenses else None


def project_url(metadata):
    """Best source-code URL from Project-URL / Home-page headers."""
    urls = {}
    for entry in metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        urls[label.strip().lower()] = url.strip()
    for key in ("source", "source code", "repository", "github", "homepage"):
        if urls.get(key):
            return urls[key]
    return (metadata.get("Home-page") or "").strip()
