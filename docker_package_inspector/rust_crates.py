"""Attribution of Rust crates statically linked into binaries in an image.

Rust code leaves no package-manager record on disk: crates are compiled into
executables and Python extension modules. Crates are recovered per host
binary from, in order of precision:

1. a cargo-auditable ``.dep-v0`` ELF section embedded in the binary,
2. a ``Cargo.lock`` shipped in the image next to the matching crate sources,
3. the ``Cargo.lock`` in the PyPI sdist of the wheel that installed the host.

Everything here is pure parsing (stdlib only, no toml dependency, Python
3.8 compatible); fetching files from the container or the network is done by
the caller. Archives are only ever read in memory, never extracted to disk.
"""

import hashlib
import io
import json
import posixpath
import re
import struct
import tarfile
import zlib

from .python_dists import extract_copyright_lines

# Lists ELF files that contain Rust standard library paths, one per line:
#   @@HOST auditable <path>   (also carries a cargo-auditable section)
#   @@HOST plain <path>
RUST_HOSTS_SCRIPT = r"""
find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o \
  -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -size +64k \
  -exec grep -l -a -F /rustc/ {} + 2>/dev/null |
while IFS= read -r f; do
  [ "$(head -c 4 "$f" 2>/dev/null)" = "$(printf '\177ELF')" ] || continue
  if grep -q -a -F .dep-v0 "$f" 2>/dev/null; then
    printf '@@HOST auditable %s\n' "$f"
  else
    printf '@@HOST plain %s\n' "$f"
  fi
done
"""

# Arguments: host paths, "--", RECORD files. For each host, emits the RECORD
# lines that mention a file with the host's basename, prefixed by the RECORD
# path:  <record path>:<record line>
RECORD_LOOKUP_SCRIPT = r"""
hosts=""
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
  hosts="$hosts
$1"
  shift
done
shift
[ "$#" -gt 0 ] || exit 0
printf '%s\n' "$hosts" | while IFS= read -r host; do
  [ -n "$host" ] || continue
  grep -H -F -- "$(basename "$host")," "$@" 2>/dev/null
done
exit 0
"""

# Prints every Cargo.lock (outside cargo's own download cache) with the
# Cargo.toml files of its project tree, and every cargo install record
# (.crates2.json):
#   @@FILE <path>
#   <content>
CARGO_FILES_SCRIPT = r"""
find / -xdev \( -path /proc -o -path /sys -o -path /dev \
  -o -path '*/.cargo/registry' -o -path '*/.cargo/git' \) -prune -o \
  -type f \( -name Cargo.lock -o -name .crates2.json \) -print 2>/dev/null |
while IFS= read -r f; do
  printf '@@FILE %s\n' "$f"
  head -c 2097152 "$f"
  printf '\n'
  [ "$(basename "$f")" = Cargo.lock ] || continue
  find "$(dirname "$f")" -maxdepth 3 -name Cargo.toml -type f 2>/dev/null |
  while IFS= read -r toml; do
    printf '@@FILE %s\n' "$toml"
    head -c 262144 "$toml"
    printf '\n'
  done
done
"""

CRATES_IO_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
_CRATES_IO_SPARSE = "sparse+https://index.crates.io/"


# ---------------------------------------------------------------------------
# Scan output parsing


def parse_hosts(output):
    """Parse RUST_HOSTS_SCRIPT output into a list of (path, is_auditable)."""
    hosts = []
    for line in output.splitlines():
        if not line.startswith("@@HOST "):
            continue
        kind, _, path = line[len("@@HOST ") :].partition(" ")
        hosts.append((path, kind == "auditable"))
    return hosts


def resolve_record_owners(output, hosts):
    """Map host paths to the dist-info directory whose RECORD lists them.

    RECORD paths are relative to the directory containing the dist-info
    (and may climb out of it, e.g. ``../../../bin/uv``), so each candidate is
    normalized and compared with the actual host path.

    Returns:
        Dict of host path -> dist-info directory path
    """
    wanted = set(hosts)
    owners = {}
    for line in output.splitlines():
        prefix, sep, entry = line.partition("/RECORD:")
        if not sep:
            continue
        record = prefix + "/RECORD"
        relative = entry.split(",", 1)[0]
        dist_info = posixpath.dirname(record)
        base = posixpath.dirname(dist_info)
        path = posixpath.normpath(posixpath.join(base, relative))
        if path in wanted and path not in owners:
            owners[path] = dist_info
    return owners


def split_files(output):
    """Parse ``@@FILE <path>`` delimited output into a dict path -> text."""
    files = {}
    current = None
    lines = []
    for line in output.splitlines():
        if line.startswith("@@FILE "):
            if current is not None:
                files[current] = "\n".join(lines)
            current = line[len("@@FILE ") :]
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        files[current] = "\n".join(lines)
    return files


def module_stem(path):
    """Python module name of an extension or binary (``_rust.abi3.so`` -> _rust)."""
    return posixpath.basename(path).split(".", 1)[0]


# ---------------------------------------------------------------------------
# cargo-auditable


def parse_auditable(elf_bytes):
    """Read the cargo-auditable dependency list embedded in an ELF binary.

    Returns:
        List of {"name", "version", "source", "root", "kind"} dicts, or None
        if the binary has no readable ``.dep-v0`` section
    """
    data = _elf_section(elf_bytes, ".dep-v0")
    if data is None:
        return None
    try:
        info = json.loads(zlib.decompress(data))
    except (zlib.error, ValueError):
        return None
    packages = []
    for pkg in info.get("packages", []):
        packages.append(
            {
                "name": pkg.get("name", ""),
                "version": pkg.get("version", ""),
                "source": pkg.get("source", "crates.io"),
                "root": bool(pkg.get("root")),
                "kind": pkg.get("kind", "runtime"),
            }
        )
    return packages


def _elf_section(elf, wanted):
    """Return the raw bytes of a named ELF section, or None."""
    if len(elf) < 64 or elf[:4] != b"\x7fELF":
        return None
    is_64 = elf[4] == 2
    endian = "<" if elf[5] == 1 else ">"
    try:
        if is_64:
            (shoff,) = struct.unpack_from(endian + "Q", elf, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", elf, 0x3A)
            section_fmt = endian + "IIQQQQIIQQ"
        else:
            (shoff,) = struct.unpack_from(endian + "I", elf, 0x20)
            shentsize, shnum, shstrndx = struct.unpack_from(endian + "HHH", elf, 0x2E)
            section_fmt = endian + "IIIIIIIIII"

        def section(index):
            fields = struct.unpack_from(section_fmt, elf, shoff + index * shentsize)
            # sh_name, sh_offset, sh_size
            return fields[0], fields[4], fields[5]

        _, strtab_offset, strtab_size = section(shstrndx)
        strtab = elf[strtab_offset : strtab_offset + strtab_size]
        for index in range(shnum):
            name_offset, offset, size = section(index)
            end = strtab.find(b"\0", name_offset)
            if strtab[name_offset:end].decode("ascii", "replace") == wanted:
                return elf[offset : offset + size]
    except struct.error:
        return None
    return None


# ---------------------------------------------------------------------------
# Cargo.lock / Cargo.toml


def parse_cargo_lock(text):
    """Parse the [[package]] entries of a Cargo.lock.

    Returns:
        List of {"name", "version", "source", "checksum", "dependencies"}
    """
    packages = []
    for block in re.split(r"^\[\[package\]\]\s*$", text, flags=re.MULTILINE)[1:]:
        block = re.split(r"^\[", block, maxsplit=1, flags=re.MULTILINE)[0]
        fields = dict(re.findall(r'^(\w+)\s*=\s*"([^"]*)"', block, re.MULTILINE))
        deps_match = re.search(r"^dependencies\s*=\s*\[(.*?)\]", block, re.S | re.M)
        deps = re.findall(r'"([^"]+)"', deps_match.group(1)) if deps_match else []
        if fields.get("name"):
            packages.append(
                {
                    "name": fields["name"],
                    "version": fields.get("version", ""),
                    "source": fields.get("source", ""),
                    "checksum": fields.get("checksum", ""),
                    "dependencies": deps,
                }
            )
    return packages


def _toml_sections(text):
    """Split TOML text into {section header: body} (top-level tables only)."""
    sections = {"": []}
    current = ""
    for line in text.splitlines():
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$", line)
        if header:
            current = header.group(1).strip()
            sections.setdefault(current, [])
        else:
            sections[current].append(line)
    return {name: "\n".join(body) for name, body in sections.items()}


def _toml_string(body, key):
    match = re.search(
        r'^\s*%s\s*=\s*(?:"([^"]*)"|\'([^\']*)\')' % re.escape(key), body, re.M
    )
    if not match:
        return ""
    return match.group(1) if match.group(1) is not None else match.group(2)


def _toml_string_list(body, key):
    match = re.search(r"^\s*%s\s*=\s*\[(.*?)\]" % re.escape(key), body, re.S | re.M)
    return re.findall(r'"([^"]*)"', match.group(1)) if match else []


def parse_cargo_toml(text):
    """Extract the fields of a Cargo.toml that attribution needs.

    Returns:
        Dict with name, lib_name, license, license_file, repository, authors,
        crate_types, normal_deps and non_runtime_deps (dev/build dependency
        package names, including target-specific ones)
    """
    sections = _toml_sections(text)
    package = sections.get("package", "")
    lib = sections.get("lib", "")

    def dep_names(kind):
        """Package names declared in [kind], [target.X.kind] and their
        dotted-table forms ([kind.foo], [target.X.kind.foo])."""
        names = set()
        for header, body in sections.items():
            if header == kind or (
                header.startswith("target.") and header.endswith("." + kind)
            ):
                # Only unindented keys: indented ones continue an inline table
                for key, value in re.findall(
                    r"^([A-Za-z0-9_-]+)\s*=\s*(.*)$", body, re.M
                ):
                    renamed = re.search(r'package\s*=\s*"([^"]+)"', value)
                    names.add(renamed.group(1) if renamed else key)
                continue
            if header.startswith(kind + "."):
                key = header[len(kind) + 1 :]
            elif header.startswith("target.") and "." + kind + "." in header:
                key = header.split("." + kind + ".", 1)[1]
            else:
                continue
            names.add(_toml_string(body, "package") or key.strip("\"'"))
        return names

    normal = dep_names("dependencies")
    non_runtime = (dep_names("dev-dependencies") | dep_names("build-dependencies")) - (
        normal
    )

    return {
        "name": _toml_string(package, "name"),
        "lib_name": _toml_string(lib, "name"),
        "license": _toml_string(package, "license"),
        "license_file": _toml_string(package, "license-file"),
        "repository": _toml_string(package, "repository")
        or _toml_string(package, "homepage"),
        "authors": _toml_string_list(package, "authors"),
        "crate_types": _toml_string_list(lib, "crate-type"),
        "normal_deps": normal,
        "non_runtime_deps": non_runtime,
    }


def local_roots(lock_packages, tomls, lock_dir):
    """Workspace crates of a Cargo.lock, paired with their Cargo.toml.

    Args:
        lock_packages: Parsed Cargo.lock packages
        tomls: Dict of Cargo.toml path -> parsed Cargo.toml
        lock_dir: Directory containing the Cargo.lock

    Returns:
        List of (crate name, parsed Cargo.toml) for the lock's local
        (sourceless) packages that have a Cargo.toml under lock_dir
    """
    local = {p["name"] for p in lock_packages if not p["source"]}
    roots = []
    for path, toml in sorted(tomls.items()):
        if path.startswith(lock_dir + "/") and toml["name"] in local:
            if toml["name"] not in [name for name, _ in roots]:
                roots.append((toml["name"], toml))
    return roots


def lock_crates(lock_text, tomls, lock_dir):
    """Runtime crates of every workspace crate of a Cargo.lock.

    When no workspace Cargo.toml is available, every registry/git package in
    the lock is returned (over-reports, never hides).

    Returns:
        List of (crate, root names) pairs, one per distinct crate version
    """
    packages = parse_cargo_lock(lock_text)
    roots = local_roots(packages, tomls, lock_dir)
    found = {}
    if not roots:
        for pkg in packages:
            if pkg["source"]:
                found[(pkg["name"], pkg["version"])] = (pkg, [])
        return list(found.values())
    for root_name, root_toml in roots:
        for pkg in runtime_closure(packages, root_name, root_toml):
            entry = found.setdefault((pkg["name"], pkg["version"]), (pkg, []))
            if root_name not in entry[1]:
                entry[1].append(root_name)
    return list(found.values())


def parse_crates2_json(text):
    """Crates installed with ``cargo install``, from a .crates2.json file.

    Keys look like ``"ripgrep 14.1.0 (registry+https://...crates.io-index)"``.

    Returns:
        List of {"name", "version", "source"} dicts
    """
    try:
        installs = json.loads(text).get("installs", {})
    except (ValueError, AttributeError):
        return []
    crates = []
    for key in installs:
        match = re.match(r"^(\S+) (\S+) \((.*)\)$", key)
        if match:
            crates.append(
                {
                    "name": match.group(1),
                    "version": match.group(2),
                    "source": match.group(3),
                }
            )
    return crates


def maturin_module_name(pyproject_text):
    """Last component of [tool.maturin] module-name in a pyproject.toml."""
    body = _toml_sections(pyproject_text).get("tool.maturin", "")
    return _toml_string(body, "module-name").rsplit(".", 1)[-1]


def root_candidates(tomls, stem, module_name="", allow_single_cdylib=False):
    """Crates in a project tree that may build the given module, best first.

    Name matches come first, cdylib crates before others: a Python binding
    crate and the core library it wraps often share a lib name.

    Args:
        tomls: Dict of Cargo.toml path -> parsed Cargo.toml
        stem: Module stem of the host binary (e.g. "tokenizers", "_rust")
        module_name: maturin module-name override, if any
        allow_single_cdylib: Accept the only cdylib crate when no name
            matches. Only safe when the files are known to belong to the
            host's project (an sdist), never for unrelated files in an image.

    Returns:
        List of Cargo.toml paths
    """
    wanted = {stem, module_name} - {""}
    matches = [
        path
        for path, toml in tomls.items()
        if {toml["lib_name"], toml["name"].replace("-", "_")} & wanted
    ]
    matches.sort(key=lambda path: "cdylib" not in tomls[path]["crate_types"])
    if matches or not allow_single_cdylib:
        return matches
    cdylibs = [path for path, toml in tomls.items() if "cdylib" in toml["crate_types"]]
    return cdylibs if len(cdylibs) == 1 else []


def runtime_closure(lock_packages, root_name, root_toml=None):
    """Packages reachable at runtime from the root crate in a Cargo.lock.

    The root's own dev-/build-dependencies are excluded. Transitive build
    dependencies (proc-macros, build scripts of dependencies) cannot be told
    apart without the full cargo metadata and are kept, so the result may
    over-report, never under-report.

    Returns:
        List of lock packages, excluding the root and path (local) crates
    """
    by_name = {}
    for pkg in lock_packages:
        by_name.setdefault(pkg["name"], []).append(pkg)

    def resolve(ref):
        parts = ref.split(" ")
        candidates = by_name.get(parts[0], [])
        if len(parts) > 1:
            candidates = [p for p in candidates if p["version"] == parts[1]]
        return candidates[0] if candidates else None

    roots = by_name.get(root_name, [])
    if not roots:
        return []
    root = roots[0]
    excluded = root_toml["non_runtime_deps"] if root_toml else set()

    seen = {id(root)}
    stack = []
    for ref in root["dependencies"]:
        if ref.split(" ")[0] not in excluded:
            dep = resolve(ref)
            if dep is not None:
                stack.append(dep)
    result = []
    while stack:
        pkg = stack.pop()
        if id(pkg) in seen:
            continue
        seen.add(id(pkg))
        if pkg["source"]:
            result.append(pkg)
        for ref in pkg["dependencies"]:
            dep = resolve(ref)
            if dep is not None and id(dep) not in seen:
                stack.append(dep)
    return sorted(result, key=lambda p: (p["name"], p["version"]))


def _governing_lock(locks, toml_path):
    """The closest Cargo.lock at or above a Cargo.toml, or None."""
    directory = posixpath.dirname(toml_path)
    while True:
        lock_path = posixpath.join(directory, "Cargo.lock")
        if lock_path in locks:
            return lock_path
        parent = posixpath.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def resolve_project_crates(files, stem, module_name="", allow_single_cdylib=False):
    """Find the runtime crates of a module in a set of project files.

    Args:
        files: Dict of path -> text for Cargo.lock / Cargo.toml files
        stem: Module stem of the host binary
        module_name: maturin module-name override, if any
        allow_single_cdylib: See root_candidates

    Returns:
        Tuple of (crate list, lock path) or (None, None) if no Cargo.lock
        in the files can be tied to the module
    """
    locks = {p: t for p, t in files.items() if posixpath.basename(p) == "Cargo.lock"}
    tomls = {
        p: parse_cargo_toml(t)
        for p, t in files.items()
        if posixpath.basename(p) == "Cargo.toml"
    }
    for root_path in root_candidates(tomls, stem, module_name, allow_single_cdylib):
        lock_path = _governing_lock(locks, root_path)
        if lock_path is None:
            continue
        root_toml = tomls[root_path]
        crates = runtime_closure(
            parse_cargo_lock(locks[lock_path]), root_toml["name"], root_toml
        )
        return crates, lock_path
    return None, None


def sdist_project_files(archive_bytes, limit=2 * 1024 * 1024):
    """Read Cargo.lock / Cargo.toml / pyproject.toml members of an sdist.

    Members under vendored dependency directories are skipped. The archive is
    read in memory; nothing is extracted.

    Returns:
        Dict of member path -> text
    """
    wanted = {"Cargo.lock", "Cargo.toml", "pyproject.toml"}
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar:
            if not member.isfile() or member.size > limit:
                continue
            if posixpath.basename(member.name) not in wanted:
                continue
            if re.search(r"(^|/)(vendor|target|tests?)/", member.name):
                continue
            handle = tar.extractfile(member)
            if handle is not None:
                files["/" + member.name] = handle.read().decode("utf-8", "replace")
    return files


# ---------------------------------------------------------------------------
# Published crate metadata


def crate_download_url(name, version):
    return f"https://static.crates.io/crates/{name}/{name}-{version}.crate"


def is_crates_io(source):
    return source in (CRATES_IO_SOURCE, "crates.io") or source.startswith(
        _CRATES_IO_SPARSE
    )


def git_repository(source):
    """Repository URL of a ``git+<url>?...#<rev>`` Cargo source, or ''."""
    if not source.startswith("git+"):
        return ""
    return re.split(r"[?#]", source[len("git+") :], maxsplit=1)[0]


def parse_crate_archive(archive_bytes, checksum="", limit=262144):
    """Read license, repository and copyright data from a published .crate.

    Args:
        archive_bytes: The .crate (tar.gz) content
        checksum: Expected sha256 from Cargo.lock; verified when given
        limit: Maximum bytes read per member

    Returns:
        Dict with license, repository, copyright, license_texts or None if the
        checksum does not match
    """
    if checksum and hashlib.sha256(archive_bytes).hexdigest() != checksum:
        return None
    toml_text = ""
    license_texts = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar:
            parts = member.name.split("/")
            if not member.isfile() or len(parts) != 2:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            if parts[1] == "Cargo.toml":
                toml_text = handle.read(limit).decode("utf-8", "replace")
            elif re.match(r"(?i)(licen[cs]e|copying|notice|copyright)", parts[1]):
                license_texts[parts[1]] = handle.read(limit).decode("utf-8", "replace")

    toml = parse_cargo_toml(toml_text) if toml_text else None
    holders = []
    for text in license_texts.values():
        for line in extract_copyright_lines(text.splitlines()):
            if line not in holders:
                holders.append(line)
    if toml:
        for author in toml["authors"]:
            if author not in holders:
                holders.append(author)
    return {
        "license": toml["license"] if toml else "",
        "license_file": toml["license_file"] if toml else "",
        "repository": toml["repository"] if toml else "",
        "copyright": holders,
        "license_texts": license_texts,
    }
