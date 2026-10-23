"""Discovery of OpenSSL and Python runtimes not installed by a package manager.

Source-built interpreters (/usr/local/bin/python3.X) and OpenSSL libraries
bundled into wheels (``<pkg>.libs/libssl-*.so``) or copied into the image
have no dpkg/rpm record, so they are found by file name. The list is
deliberately narrow: a general inventory of unowned ELF files would be too
noisy to review.
"""

import re

# One line per candidate file, tab separated:
#   @@RUNTIME <kind> <version> <path>
RUNTIME_SCAN_SCRIPT = r"""
find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o \
  -type f \( -name 'libssl.so*' -o -name 'libssl-*.so*' \
  -o -name 'libcrypto.so*' -o -name 'libcrypto-*.so*' \
  -o -name python3 -o -name 'python3.[0-9]*' \) -print 2>/dev/null |
while IFS= read -r f; do
  case "$(basename "$f")" in
    libssl*|libcrypto*)
      v=$(grep -a -o -m 1 'OpenSSL [0-9][0-9a-z.]*' "$f" 2>/dev/null |
        head -n 1 | cut -d' ' -f2)
      printf '@@RUNTIME\topenssl\t%s\t%s\n' "$v" "$f"
      ;;
    *-config|*.py|*.pyc|*.gdb|*.so*) ;;
    *)
      [ -x "$f" ] || continue
      v=$("$f" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' \
        2>/dev/null)
      [ -n "$v" ] || continue
      printf '@@RUNTIME\tpython\t%s\t%s\n' "$v" "$f"
      printf '@@PREFIX\t%s\t%s\n' "$f" \
        "$("$f" -c 'import sys; print(sys.base_prefix)' 2>/dev/null)"
      ;;
  esac
done
"""

RUNTIME_SOURCES = {
    "openssl": "https://www.openssl.org/source/",
    "python": "https://www.python.org/downloads/source/",
}


def parse_runtime_scan(output):
    """Parse RUNTIME_SCAN_SCRIPT output.

    Returns:
        List of {"kind", "version", "path", "prefix"} dicts
    """
    runtimes = []
    prefixes = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if fields[0] == "@@RUNTIME" and len(fields) == 4:
            runtimes.append(
                {"kind": fields[1], "version": fields[2], "path": fields[3]}
            )
        elif fields[0] == "@@PREFIX" and len(fields) == 3:
            prefixes[fields[1]] = fields[2]
    for runtime in runtimes:
        runtime["prefix"] = prefixes.get(runtime["path"], "")
    return runtimes


def python_license_candidates(runtime):
    """Where a source-built CPython installs its LICENSE.txt."""
    match = re.match(r"(\d+\.\d+)", runtime["version"])
    if not runtime["prefix"] or not match:
        return []
    return [f"{runtime['prefix']}/lib/python{match.group(1)}/LICENSE.txt"]
