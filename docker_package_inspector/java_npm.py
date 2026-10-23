"""Discovery of Java (Maven) and npm packages installed in an image.

Java libraries are found inside jar files, including jar-with-dependencies
("shaded") jars that bundle many artifacts, through the
``META-INF/maven/<group>/<artifact>/pom.properties`` entries Maven writes.
npm packages are found from the ``package.json`` of every installed module
under ``node_modules``. Only installed trees are read, never
``devDependencies`` declarations, so test-only packages are not reported
unless they are actually installed.

Parsing is stdlib-only and in memory. pom.xml is read with regular
expressions rather than an XML parser, since the content is untrusted.
"""

import io
import json
import posixpath
import re
import zipfile

from .python_dists import extract_copyright_lines

_PRUNE = r"\( -path /proc -o -path /sys -o -path /dev \) -prune -o"

# One path per line
JAR_SCAN_SCRIPT = (
    r"find / -xdev " + _PRUNE + r" -type f -name '*.jar' -size +0 -print 2>/dev/null"
)

# For each installed npm module (and each top-level application that has a
# node_modules directory next to its package.json):
#   @@PKG <directory>
#   <package.json content>
#   @@LICENSE_FILE <path>     (verified non-empty, repeated)
#   @@COPYRIGHT <line>        (repeated)
#   @@END
NPM_SCAN_SCRIPT = (
    r"find / -xdev "
    + _PRUNE
    + r""" -type f -name package.json -print 2>/dev/null |
while IFS= read -r f; do
  d=$(dirname "$f")
  case "$d" in
    */node_modules/*) ;;
    *) [ -d "$d/node_modules" ] || continue ;;
  esac
  printf '@@PKG %s\n' "$d"
  head -c 65536 "$f"
  printf '\n'
  for l in "$d"/LICEN[CS]E* "$d"/licen[cs]e* "$d"/COPYING* "$d"/NOTICE*; do
    if [ -f "$l" ] && [ -s "$l" ]; then
      printf '@@LICENSE_FILE %s\n' "$l"
      grep -iE 'copyright|\(c\)' "$l" 2>/dev/null | head -n 20 |
        sed 's/^/@@COPYRIGHT /'
    fi
  done
  printf '@@END\n'
done
"""
)

_MODULE_DIR_RE = re.compile(r"/node_modules/(@[^/]+/)?[^/]+$")
_M2_JAR_RE = re.compile(
    r"/\.m2/repository/(?P<group>.+)/(?P<artifact>[^/]+)/(?P<version>[^/]+)/"
    r"(?P=artifact)-(?P=version)(-[^/]*)?\.jar$"
)


# ---------------------------------------------------------------------------
# Java


def maven_coordinates_from_m2_path(path):
    """(group, artifact, version) of a jar in a local Maven repository."""
    match = _M2_JAR_RE.search(path)
    if not match:
        return None
    return (
        match.group("group").replace("/", "."),
        match.group("artifact"),
        match.group("version"),
    )


def _xml_text(pattern, text):
    match = re.search(pattern, text, re.S)
    return " ".join(match.group(1).split()) if match else ""


def parse_pom(text):
    """Licenses, parent coordinates and SCM URL from a pom.xml.

    Returns:
        Dict with licenses (list of names), parent ((g, a, v) or None) and url
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    licenses = []
    block = _xml_text(r"<licenses>(.*?)</licenses>", text)
    for license_block in re.findall(r"<license>(.*?)</license>", block, re.S):
        name = _xml_text(r"<name>(.*?)</name>", license_block) or _xml_text(
            r"<url>(.*?)</url>", license_block
        )
        if name and name not in licenses:
            licenses.append(name)

    parent = None
    parent_block = _xml_text(r"<parent>(.*?)</parent>", text)
    if parent_block:
        coordinates = tuple(
            _xml_text(rf"<{tag}>(.*?)</{tag}>", parent_block)
            for tag in ("groupId", "artifactId", "version")
        )
        if all(coordinates):
            parent = coordinates

    scm = _xml_text(r"<scm>(.*?)</scm>", text)
    url = _xml_text(r"<url>(.*?)</url>", scm) if scm else ""
    return {"licenses": licenses, "parent": parent, "url": url}


def maven_pom_url(group, artifact, version):
    return (
        "https://repo1.maven.org/maven2/"
        f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"
    )


def _properties(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def _manifest_headers(text):
    """Headers of a jar MANIFEST.MF; a line starting with one space continues
    the previous header's value."""
    headers = {}
    key = None
    for line in text.splitlines():
        if line.startswith(" ") and key:
            headers[key] += line[1:]
        elif ": " in line:
            key, _, value = line.partition(": ")
            headers[key] = value
        else:
            key = None
    return {k: v.strip() for k, v in headers.items()}


def parse_jar(jar_bytes, limit=1024 * 1024):
    """Artifacts bundled in a jar, with their embedded pom.xml and licenses.

    Returns:
        Dict with artifacts (list of {group, artifact, version, pom}),
        license_entries (names of META-INF license/notice entries),
        copyright (holder lines from those entries) and manifest
        ({title, version, license} from MANIFEST.MF; license is the OSGi
        Bundle-License header)
    """
    artifacts = []
    license_entries = []
    copyright_lines = []
    manifest = {}
    with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
        names = jar.namelist()

        def read(name):
            info = jar.getinfo(name)
            if info.file_size > limit:
                return ""
            return jar.read(name).decode("utf-8", "replace")

        for name in names:
            if re.match(r"META-INF/maven/[^/]+/[^/]+/pom\.properties$", name):
                props = _properties(read(name))
                pom_name = name[: -len("pom.properties")] + "pom.xml"
                if props.get("artifactId") and props.get("version"):
                    artifacts.append(
                        {
                            "group": props.get("groupId", ""),
                            "artifact": props["artifactId"],
                            "version": props["version"],
                            "pom": read(pom_name) if pom_name in names else "",
                        }
                    )
            elif re.match(r"(?i)META-INF/(licen[cs]e|notice|copying)[^/]*$", name):
                license_entries.append(name)
                copyright_lines.extend(read(name).splitlines())
            elif name == "META-INF/MANIFEST.MF":
                headers = _manifest_headers(read(name))
                manifest = {
                    "title": headers.get("Implementation-Title")
                    or headers.get("Bundle-SymbolicName", "").split(";")[0],
                    "version": headers.get("Implementation-Version")
                    or headers.get("Bundle-Version", ""),
                    "license": headers.get("Bundle-License", ""),
                }
    return {
        "artifacts": artifacts,
        "license_entries": license_entries,
        "copyright": extract_copyright_lines(copyright_lines),
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# npm


def _license_of(manifest):
    """License string from package.json license / legacy licenses fields."""
    value = manifest.get("license")
    if isinstance(value, dict):
        value = value.get("type")
    if isinstance(value, str) and value.strip():
        return value.strip()
    legacy = manifest.get("licenses")
    if isinstance(legacy, list):
        names = []
        for entry in legacy:
            name = entry.get("type") if isinstance(entry, dict) else entry
            if isinstance(name, str) and name not in names:
                names.append(name)
        if names:
            return " OR ".join(names)
    return ""


def _person(value):
    if isinstance(value, dict):
        return value.get("name", "")
    return value if isinstance(value, str) else ""


def _repository_url(manifest):
    repo = manifest.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url", "")
    if not isinstance(repo, str):
        return ""
    repo = re.sub(r"^git\+", "", repo)
    if re.match(r"^[\w.-]+/[\w.-]+$", repo):
        return f"https://github.com/{repo}"
    return repo


def parse_npm_scan(output):
    """Parse NPM_SCAN_SCRIPT output into per-directory package dicts.

    Directories whose package.json lacks a name or version, or that are not
    a module root (nested package.json files used for exports), are skipped.

    Returns:
        List of dicts with name, version, license, author, repository,
        directory, license_files, copyright and is_module
    """
    packages = []
    current = None
    body = []
    license_files = []
    copyright_lines = []
    in_body = False
    for line in output.splitlines():
        if line.startswith("@@PKG "):
            current = line[len("@@PKG ") :]
            body, license_files, copyright_lines = [], [], []
            in_body = True
        elif current is None:
            continue
        elif line.startswith("@@LICENSE_FILE "):
            in_body = False
            license_files.append(line[len("@@LICENSE_FILE ") :])
        elif line.startswith("@@COPYRIGHT "):
            in_body = False
            copyright_lines.append(line[len("@@COPYRIGHT ") :])
        elif line == "@@END":
            package = _npm_package(current, "\n".join(body))
            if package is not None:
                package["license_files"] = license_files
                package["copyright"] = extract_copyright_lines(copyright_lines)
                if package["author"] and package["author"] not in package["copyright"]:
                    package["copyright"].append(package["author"])
                packages.append(package)
            current = None
            in_body = False
        elif in_body:
            body.append(line)
    return packages


def _npm_package(directory, text):
    try:
        manifest = json.loads(text)
    except ValueError:
        return None
    if not isinstance(manifest, dict):
        return None
    name, version = manifest.get("name"), manifest.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    is_module = "/node_modules/" in directory
    if is_module and not _MODULE_DIR_RE.search(directory):
        return None
    return {
        "name": name,
        "version": version,
        "license": _license_of(manifest),
        "author": _person(manifest.get("author")),
        "repository": _repository_url(manifest),
        "directory": directory,
        "is_module": is_module,
    }


def jar_display_name(path):
    """Fallback artifact name for a jar without Maven metadata."""
    return re.sub(r"(-[0-9][^/]*)?\.jar$", "", posixpath.basename(path))
