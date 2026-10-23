"""Docker image inspection logic."""

import io
import json
import re
import signal
import subprocess
import sys
import tarfile

import docker
from docker.errors import APIError, DockerException, ImageNotFound

from .java_npm import (
    JAR_SCAN_SCRIPT,
    NPM_SCAN_SCRIPT,
    jar_display_name,
    maven_coordinates_from_m2_path,
    maven_pom_url,
    parse_jar,
    parse_npm_scan,
    parse_pom,
)
from .package_metadata import PackageMetadataFetcher
from .python_dists import (
    DIST_SCAN_SCRIPT,
    dist_info_version,
    extract_copyright_lines,
    group_dists,
    local_license,
    local_license_fallback,
    normalize_name,
    parse_dist_scan,
    project_url,
)
from .runtimes import (
    RUNTIME_SCAN_SCRIPT,
    RUNTIME_SOURCES,
    parse_runtime_scan,
    python_license_candidates,
)
from .rust_crates import (
    CARGO_FILES_SCRIPT,
    RECORD_LOOKUP_SCRIPT,
    RUST_HOSTS_SCRIPT,
    git_repository,
    is_crates_io,
    maturin_module_name,
    module_stem,
    parse_auditable,
    parse_hosts,
    resolve_project_crates,
    resolve_record_owners,
    sdist_project_files,
    split_files,
)

# Fields every package row carries, with their defaults, so JSON and CSV
# consumers can rely on them regardless of which extractor produced the row
PACKAGE_DEFAULTS = {
    "install_path": list,
    "license_files": list,
    "copyright": list,
    "is_private_env": lambda: False,
    "owning_dpkg": str,
}

# DEP-5 copyright files of large packages list hundreds of holders
MAX_COPYRIGHT_LINES = 50


_DEP5_LICENSE_RE = re.compile(r"^License:[ \t]*(\S.*)$", re.MULTILINE)
_DEP5_OPERATOR_RE = re.compile(r",?\s+(or|and)\s+", re.IGNORECASE)
_DEP5_OPERAND_SPLIT_RE = re.compile(r",?\s+(?:or|and|with)\s+", re.IGNORECASE)
_DEP5_OPERAND_RE = re.compile(r"^[^\s,;]+(\s+exception)?$", re.IGNORECASE)


def dep5_license_names(content):
    """License expressions named on the "License:" lines of a copyright file.

    Only the first line of each License field is the license name; the
    continuation lines are license text. DEP-5 "or"/"and" (optionally
    preceded by a comma for precedence) become SPDX-style OR/AND. Lines
    whose value is not made of license short names (some free-form files
    put license text right after "License:") are ignored.

    Returns:
        De-duplicated list of expressions, in file order
    """
    names = []
    for value in _DEP5_LICENSE_RE.findall(content):
        value = value.strip(" ,.;")
        operands = _DEP5_OPERAND_SPLIT_RE.split(value)
        if not all(_DEP5_OPERAND_RE.match(o.strip("() ")) for o in operands):
            continue
        name = _DEP5_OPERATOR_RE.sub(lambda m: f" {m.group(1).upper()} ", value)
        name = " ".join(name.split())
        if name and name not in names:
            names.append(name)
    return names


def select_platform_digest(manifest, architecture, variant=""):
    """Pick the manifest digest for a platform from a manifest list / index.

    Args:
        manifest: Parsed `docker manifest inspect` output
        architecture: Wanted architecture (e.g. amd64, arm64)
        variant: Wanted variant (e.g. v8); ignored when the entry has none

    Returns:
        The digest, "" if the index has no matching linux entry, or None if
        the manifest is not an index
    """
    entries = manifest.get("manifests")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        platform = entry.get("platform") or {}
        if platform.get("os") != "linux":
            continue
        if platform.get("architecture") != architecture:
            continue
        if variant and platform.get("variant") and platform["variant"] != variant:
            continue
        return entry.get("digest", "")
    return ""


class DockerImageInspector:
    """Inspects Docker images and extracts package information."""

    def __init__(self, verbose=False, scan_rust=True):
        """Initialize the inspector.

        Args:
            verbose: Enable verbose logging
            scan_rust: Attribute Rust crates linked into binaries in the image
        """
        self.verbose = verbose
        self.scan_rust = scan_rust
        self.client = None
        self.metadata_fetcher = PackageMetadataFetcher()

    def _log(self, message):
        """Log a message if verbose mode is enabled."""
        if self.verbose:
            print(f"[INFO] {message}", file=sys.stderr)

    def _get_docker_client(self):
        """Get or create Docker client."""
        if self.client is None:
            try:
                self.client = docker.from_env()
                self._log("Connected to Docker daemon")
            except DockerException as e:
                raise Exception(f"Failed to connect to Docker daemon: {e}")
        return self.client

    def inspect_image(self, image_name, architecture=None, pull=True):
        """Inspect a Docker image and extract package information.

        Args:
            image_name: Name of the Docker image
            architecture: Target architecture (amd64, arm64, etc.)
            pull: Whether to pull the image if not available

        Returns:
            Dictionary with python_packages and binary_packages lists
        """
        client = self._get_docker_client()

        # Pull image if requested
        pulled = False
        if pull:
            self._log(f"Pulling image: {image_name}")
            try:
                platform = f"linux/{architecture}" if architecture else None
                client.images.pull(image_name, platform=platform)
                pulled = True
                self._log("Image pulled successfully")
            except Exception as e:
                self._log(f"Warning: Failed to pull image: {e}")

        # Get image
        try:
            image = client.images.get(image_name)
        except ImageNotFound:
            raise Exception(f"Image not found: {image_name}. Try with --pull flag.")

        # Get image architecture and digest
        image_arch = image.attrs.get("Architecture", "unknown")
        image_digest = ""
        index_digest = ""
        if image.attrs.get("RepoDigests"):
            full_digest = (
                image.attrs["RepoDigests"][0] if image.attrs["RepoDigests"] else ""
            )
            # Extract only the checksum part (after @)
            if "@" in full_digest:
                image_digest = full_digest.split("@", 1)[1]
            else:
                image_digest = full_digest
            image_digest, index_digest = self._platform_digest(
                full_digest, image_digest, image_arch, image.attrs.get("Variant", "")
            )
        elif pulled:
            # The classic image store records RepoDigests for only one
            # platform per tag, so a second architecture pulled under the
            # same tag has none: resolve it from the tag just pulled
            self._log("No RepoDigests for this platform, resolving by tag")
            image_digest, _ = self._platform_digest(
                image_name, "", image_arch, image.attrs.get("Variant", "")
            )
        self._log(f"Image architecture: {image_arch}")
        self._log(f"Image digest: {image_digest}")
        if index_digest:
            self._log(f"Image index digest: {index_digest}")

        # Create and run container
        self._log("Creating temporary container...")
        container = None
        try:
            container = client.containers.create(
                image_name, command="sleep infinity", detach=True
            )

            # Start the container
            container.start()
            self._log("Container started")

            python_packages, python_deps = self._extract_python_packages(container)
            binary_packages = self._extract_binary_packages(container)

            # Mark packages with dependency information
            all_packages = []

            # Process Python packages
            for pkg in python_packages:
                pkg["package_type"] = "python"
                pkg["is_dependency"] = False
                pkg["parent_packages"] = []
                all_packages.append(pkg)

            # Process binary packages
            for pkg in binary_packages:
                pkg["package_type"] = "binary"
                pkg["is_dependency"] = False
                pkg["parent_packages"] = []
                all_packages.append(pkg)

            # Mark Python dependencies
            python_by_name = {
                normalize_name(pkg["name"]): pkg
                for pkg in all_packages
                if pkg["package_type"] == "python"
            }
            for pkg_name, deps in python_deps.items():
                for dep in deps:
                    pkg = python_by_name.get(normalize_name(dep))
                    if pkg is not None:
                        pkg["is_dependency"] = True
                        if pkg_name not in pkg["parent_packages"]:
                            pkg["parent_packages"].append(pkg_name)

            all_packages.extend(
                self._extract_unowned_runtimes(container, python_packages)
            )
            all_packages.extend(self._extract_java_packages(container))
            all_packages.extend(self._extract_npm_packages(container))

            if self.scan_rust:
                all_packages.extend(
                    self._extract_rust_crates(container, python_packages)
                )

            for pkg in all_packages:
                for field, default in PACKAGE_DEFAULTS.items():
                    pkg.setdefault(field, default())

            self._link_python_to_dpkg(container, all_packages)

            # Post-process: Check source URLs for packages with unknown licenses
            self._enrich_unknown_licenses(all_packages, container)

            return {
                "digest": image_digest,
                "index_digest": index_digest,
                "architecture": image_arch,
                "packages": all_packages,
            }

        except KeyboardInterrupt:
            self._log("Interrupted by user, cleaning up container...")
            raise
        except Exception as e:
            raise Exception(f"Failed to inspect container: {e}")
        finally:
            # Ensure container is always removed, even on interrupt
            if container is not None:
                try:
                    container.remove(force=True)
                    self._log("Temporary container removed")
                except Exception as cleanup_error:
                    self._log(f"Warning: Failed to remove container: {cleanup_error}")

    def _platform_digest(self, repo_digest_ref, repo_digest, architecture, variant):
        """Resolve the per-platform manifest digest of a pulled image.

        RepoDigests holds the digest the image was pulled by, which for a
        multi-arch image is the index (manifest list) digest, identical for
        every architecture. The index is inspected by that exact digest (not
        the tag, which may have moved since the pull) and the entry for the
        inspected platform is selected.

        Args:
            repo_digest_ref: "<repository>@<digest>" from RepoDigests, or the
                pulled tag when the image has no RepoDigests
            repo_digest: The digest part of repo_digest_ref ("" for a tag)
            architecture: Architecture of the pulled image (e.g. arm64)
            variant: Architecture variant of the pulled image (e.g. v8)

        Returns:
            Tuple of (platform digest, index digest). The index digest is ""
            for a single-platform image. On any failure the repo digest is
            returned unchanged with an empty index digest.
        """
        if not repo_digest_ref:
            return repo_digest, ""
        try:
            result = subprocess.run(
                ["docker", "manifest", "inspect", repo_digest_ref],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._log(f"Warning: docker manifest inspect unavailable: {e}")
            return repo_digest, ""
        if result.returncode != 0:
            self._log(
                "Warning: docker manifest inspect failed, digest may be the "
                f"index digest: {result.stderr.strip()}"
            )
            return repo_digest, ""
        try:
            manifest = json.loads(result.stdout)
        except ValueError:
            self._log("Warning: unreadable docker manifest inspect output")
            return repo_digest, ""

        platform_digest = select_platform_digest(manifest, architecture, variant)
        if platform_digest is None:
            # Not an index: the pulled digest already is the platform manifest
            return repo_digest, ""
        if not platform_digest:
            self._log(
                f"Warning: no {architecture} entry in index {repo_digest}, "
                "keeping the index digest"
            )
            return repo_digest, ""
        return platform_digest, repo_digest

    def _extract_python_packages(self, container):
        """Extract Python packages and their dependencies from container.

        Distributions are discovered from dist-info/egg-info directories, which
        covers every installer (pip, uv, ...) and environment. pip is used only
        as a fallback when the filesystem scan finds nothing.

        Args:
            container: Docker container object

        Returns:
            Tuple of (list of Python package dictionaries,
            dict mapping package names to their dependencies)
        """
        self._log("Extracting Python packages...")
        dists = self._scan_python_dists(container)
        if not dists:
            self._log("No dist-info found, falling back to pip list")
            return (
                self._extract_pip_packages(container),
                self._get_python_dependencies(container),
            )

        packages = []
        dependencies = {}
        for group in group_dists(dists):
            name = group["name"]
            metadata = group["primary"]["metadata"]
            pypi = self.metadata_fetcher.get_pypi_metadata(
                name, metadata.get("Version", "")
            )

            license_info, license_source = local_license(metadata)
            if not license_info:
                license_info = pypi.get("license", "Unknown")
                license_source = pypi.get("license_source", "PyPI API")
            if license_info == "Unknown":
                fallback = local_license_fallback(metadata)
                if fallback:
                    license_info = fallback
                    license_source = "METADATA License/Classifier"

            packages.append(
                {
                    "name": name,
                    "version": group["version"],
                    "source": pypi.get("source", f"https://pypi.org/project/{name}/"),
                    "license": license_info,
                    "license_source": license_source,
                    "source_code_url": pypi.get("source_code_url")
                    or project_url(metadata),
                    "package_provider": group["provider"],
                    "install_path": group["paths"],
                    "license_files": group["license_files"],
                    "copyright": group["copyright"],
                    "is_private_env": group["is_private_env"],
                }
            )
            if group["requires"]:
                dependencies[name] = group["requires"]

        self._log(f"Found {len(packages)} Python packages")
        return packages, dependencies

    def _exec_text(self, container, script, *args):
        """Run a /bin/sh script in the container and return its stdout.

        The exit status is ignored (grep/find pipelines exit non-zero when
        nothing matches); an exec failure returns an empty string.
        """
        try:
            _, output = container.exec_run(
                ["sh", "-c", script, "sh"] + list(args), demux=True
            )
        except Exception as e:
            self._log(f"Warning: exec in container failed: {e}")
            return ""
        stdout = output[0] if isinstance(output, tuple) else output
        return (stdout or b"").decode("utf-8", errors="ignore")

    def _read_container_file(self, container, path, max_bytes=512 * 1024 * 1024):
        """Copy one file out of the container into memory.

        Returns:
            File content as bytes, or None if unreadable or larger than max_bytes
        """
        try:
            stream, stat = container.get_archive(path)
            if stat.get("size", 0) > max_bytes:
                return None
            archive = io.BytesIO()
            for chunk in stream:
                archive.write(chunk)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as tar:
                member = tar.next()
                handle = tar.extractfile(member) if member else None
                return handle.read() if handle else None
        except Exception as e:
            self._log(f"  Warning: Failed to copy {path}: {e}")
            return None

    def _extract_rust_crates(self, container, python_packages):
        """Attribute Rust crates to the binaries that statically link them.

        Args:
            container: Docker container object
            python_packages: Python package rows, used to find which wheel
                installed each Rust binary

        Returns:
            List of package dictionaries: one "rust" row per crate, and one
            "rust-binary" row per Rust binary whose crates could not be
            resolved
        """
        self._log("Scanning for Rust binaries...")
        hosts = parse_hosts(self._exec_text(container, RUST_HOSTS_SCRIPT))
        if not hosts:
            return []
        self._log(f"Found {len(hosts)} Rust binaries")

        host_owner = self._record_owners(
            container, [path for path, _ in hosts], python_packages
        )
        cargo_files = split_files(self._exec_text(container, CARGO_FILES_SCRIPT))

        crates = {}
        unresolved = []
        sdist_cache = {}
        for path, auditable in hosts:
            owner = host_owner.get(path)
            found, provider = None, None

            if auditable:
                found = self._auditable_crates(container, path)
                provider = "cargo-auditable"
            if found is None and cargo_files:
                found, lock = resolve_project_crates(cargo_files, module_stem(path))
                provider = f"Cargo.lock(image): {lock}"
            if found is None and owner is not None:
                key = (owner["name"], owner["version"])
                if key not in sdist_cache:
                    sdist_cache[key] = self._sdist_files(*key)
                files = sdist_cache[key]
                if files:
                    module_name = next(
                        (
                            maturin_module_name(text)
                            for name, text in sorted(files.items())
                            if name.endswith("/pyproject.toml")
                        ),
                        "",
                    )
                    found, _ = resolve_project_crates(
                        files, module_stem(path), module_name, allow_single_cdylib=True
                    )
                    provider = f"Cargo.lock(sdist): {owner['name']}=={owner['version']}"

            if found is None:
                unresolved.append(self._unresolved_rust_binary(path, owner))
                continue
            self._log(f"  {path}: {len(found)} crates via {provider}")
            for crate in found:
                self._add_crate(crates, crate, path, provider.split(":")[0])

        self._log(
            f"Resolved {len(crates)} crates; {len(unresolved)} Rust binaries "
            "unresolved"
        )
        return sorted(crates.values(), key=lambda c: c["name"]) + unresolved

    def _record_owners(self, container, paths, python_packages):
        """Map file paths to the Python package whose RECORD lists them.

        Returns:
            Dict of path -> {"name", "version"} of the installing wheel
        """
        dist_to_pkg = {}
        for pkg in python_packages:
            for dist_path in pkg.get("install_path", []):
                if dist_path.endswith(".dist-info"):
                    dist_to_pkg[dist_path] = pkg
        if not dist_to_pkg or not paths:
            return {}
        output = self._exec_text(
            container,
            RECORD_LOOKUP_SCRIPT,
            *(paths + ["--"] + [d + "/RECORD" for d in dist_to_pkg]),
        )
        owners = {}
        for host, dist_info in resolve_record_owners(output, paths).items():
            pkg = dist_to_pkg[dist_info]
            version = dist_info_version(dist_info) or pkg["version"].split(", ")[0]
            owners[host] = {"name": pkg["name"], "version": version}
        return owners

    def _extract_unowned_runtimes(self, container, python_packages):
        """Report OpenSSL and Python runtimes that no Debian package installed.

        Copies bundled in a wheel name that wheel in parent_packages. The
        license is taken only from a license file found next to the runtime;
        otherwise it stays Unknown.

        Returns:
            List of package dictionaries (package_provider "unowned")
        """
        self._log("Looking for unowned OpenSSL/Python runtimes...")
        runtimes = parse_runtime_scan(self._exec_text(container, RUNTIME_SCAN_SCRIPT))
        owned = self._dpkg_owners(container, [r["path"] for r in runtimes])
        runtimes = [r for r in runtimes if r["path"] not in owned]
        if not runtimes:
            return []
        wheel_owners = self._record_owners(
            container, [r["path"] for r in runtimes], python_packages
        )

        rows = {}
        for runtime in runtimes:
            license_files = []
            if runtime["kind"] == "python":
                candidates = python_license_candidates(runtime)
                license_files = self._exec_text(
                    container,
                    'for f; do [ -f "$f" ] && [ -s "$f" ] && printf "%s\\n" "$f"; done',
                    *candidates,
                ).splitlines()

            row = rows.get(runtime["kind"])
            if row is None:
                row = rows[runtime["kind"]] = {
                    "name": runtime["kind"],
                    "version": runtime["version"],
                    "source": RUNTIME_SOURCES.get(runtime["kind"], ""),
                    "license": "Unknown",
                    "license_source": "No license file next to the runtime",
                    "source_code_url": RUNTIME_SOURCES.get(runtime["kind"], ""),
                    "package_provider": "unowned",
                    "package_type": "binary",
                    "is_dependency": False,
                    "parent_packages": [],
                    "install_path": [],
                    "license_files": [],
                }
            versions = [v for v in row["version"].split(", ") if v]
            if runtime["version"] and runtime["version"] not in versions:
                row["version"] = ", ".join(sorted(versions + [runtime["version"]]))
            row["install_path"].append(runtime["path"])
            owner = wheel_owners.get(runtime["path"])
            if owner and owner["name"] not in row["parent_packages"]:
                row["parent_packages"].append(owner["name"])
            for path in license_files:
                if path not in row["license_files"]:
                    row["license_files"].append(path)

        for row in rows.values():
            if row["license_files"]:
                result = self._license_from_files(container, row["license_files"])
                if result:
                    row["license"], row["license_source"] = result
        self._log(f"Found {len(rows)} unowned runtimes")
        return list(rows.values())

    def _auditable_crates(self, container, path):
        """Runtime crates recorded by cargo-auditable in a binary, or None."""
        elf = self._read_container_file(container, path)
        deps = parse_auditable(elf) if elf else None
        if deps is None:
            return None
        return [
            {"name": d["name"], "version": d["version"], "source": d["source"]}
            for d in deps
            if not d["root"] and d["kind"] != "build" and d["source"] != "local"
        ]

    def _sdist_files(self, name, version):
        """Cargo/pyproject files from the PyPI sdist of a release, or {}."""
        url = self.metadata_fetcher.get_pypi_sdist_url(name, version)
        if not url:
            return {}
        archive = self.metadata_fetcher.fetch_bytes(url)
        if not archive:
            return {}
        try:
            return sdist_project_files(archive)
        except Exception as e:
            self._log(f"  Warning: Failed to read sdist {url}: {e}")
            return {}

    def _add_crate(self, crates, crate, host_path, provider):
        """Merge one crate occurrence into the per-name crate rows."""
        name, version, source = crate["name"], crate["version"], crate["source"]
        if is_crates_io(source):
            meta = self.metadata_fetcher.get_crate_metadata(
                name, version, crate.get("checksum", "")
            )
            url = f"https://crates.io/crates/{name}/{version}"
        else:
            meta = {
                "license": "Unknown",
                "license_source": "Not a crates.io crate",
                "repository": git_repository(source),
                "copyright": [],
            }
            url = git_repository(source) or source

        row = crates.get(name)
        if row is None:
            row = crates[name] = {
                "name": name,
                "version": version,
                "source": url,
                "license": meta["license"],
                "license_source": meta["license_source"],
                "source_code_url": meta["repository"],
                "package_provider": provider,
                "package_type": "rust",
                "is_dependency": True,
                "parent_packages": [],
                "copyright": [],
            }
        else:
            versions = row["version"].split(", ")
            if version not in versions:
                row["version"] = ", ".join(sorted(versions + [version]))
            licenses = row["license"].split(" | ")
            if meta["license"] not in licenses:
                row["license"] = " | ".join(licenses + [meta["license"]])
            providers = row["package_provider"].split(", ")
            if provider not in providers:
                row["package_provider"] = ", ".join(sorted(providers + [provider]))
        if host_path not in row["parent_packages"]:
            row["parent_packages"].append(host_path)
        for line in meta["copyright"]:
            if line not in row["copyright"]:
                row["copyright"].append(line)

    @staticmethod
    def _unresolved_rust_binary(path, owner):
        """Row for a Rust binary whose crates could not be determined."""
        return {
            "name": path.rsplit("/", 1)[-1],
            "version": owner["version"] if owner else "",
            "source": "",
            "license": "Unknown",
            "license_source": "No cargo-auditable data or Cargo.lock found",
            "source_code_url": "",
            "package_provider": "unresolved",
            "package_type": "rust-binary",
            "is_dependency": False,
            "parent_packages": [owner["name"]] if owner else [],
            "install_path": [path],
        }

    def _dpkg_owners(self, container, paths):
        """Map paths to the Debian package that installed them (one call)."""
        paths = [p for p in paths if p.startswith("/usr/") or p.startswith("/opt/")]
        if not paths:
            return {}
        try:
            _, output = container.exec_run(["dpkg-query", "-S"] + paths, demux=False)
        except Exception as e:
            self._log(f"Warning: dpkg ownership lookup failed: {e}")
            return {}
        owners = {}
        for line in (output or b"").decode("utf-8", errors="ignore").splitlines():
            if line.startswith("diversion by "):
                continue
            names, sep, path = line.partition(": ")
            if sep and path.strip() in paths:
                owners[path.strip()] = names.split(",")[0].strip().split(":")[0]
        return owners

    def _extract_java_packages(self, container):
        """Extract Maven artifacts from the jar files in the container.

        Every artifact bundled in a jar (including shaded jar-with-dependencies
        builds) becomes a "java" row whose parent_packages names the jar. A
        jar's META-INF license files are credited to its artifact only when
        the jar holds a single artifact: in a shaded jar they belong to one
        of the bundled projects and say nothing about the others.

        Returns:
            List of package dictionaries
        """
        self._log("Extracting Java packages...")
        jar_paths = self._exec_text(container, JAR_SCAN_SCRIPT).splitlines()
        if not jar_paths:
            return []
        owners = self._dpkg_owners(container, jar_paths)

        rows = {}
        for path in jar_paths:
            coordinates = maven_coordinates_from_m2_path(path)
            if coordinates:
                group, artifact, version = coordinates
                pom_path = path.rsplit("/", 1)[0] + f"/{artifact}-{version}.pom"
                pom = self._exec_text(container, 'cat "$1"', pom_path)
                jar = {
                    "artifacts": [
                        {
                            "group": group,
                            "artifact": artifact,
                            "version": version,
                            "pom": pom,
                        }
                    ],
                    "license_entries": [],
                    "copyright": [],
                }
            else:
                data = self._read_container_file(container, path)
                try:
                    jar = parse_jar(data) if data else None
                except Exception as e:
                    self._log(f"  Warning: Failed to read jar {path}: {e}")
                    jar = None
                if jar is None:
                    continue
                if not jar["artifacts"]:
                    manifest = jar["manifest"]
                    jar["artifacts"] = [
                        {
                            "group": "",
                            "artifact": manifest.get("title") or jar_display_name(path),
                            "version": manifest.get("version", ""),
                            "pom": "",
                            "manifest_license": manifest.get("license", ""),
                        }
                    ]

            single = len(jar["artifacts"]) == 1
            for art in jar["artifacts"]:
                self._add_java_row(
                    rows,
                    art,
                    path,
                    license_files=(
                        [f"{path}!/{entry}" for entry in jar["license_entries"]]
                        if single
                        else []
                    ),
                    copyright_lines=jar["copyright"] if single else [],
                    private=coordinates is not None,
                    owner=owners.get(path, ""),
                )

        self._log(f"Found {len(rows)} Java artifacts in {len(jar_paths)} jars")
        return sorted(rows.values(), key=lambda r: r["name"])

    def _add_java_row(
        self, rows, art, jar_path, license_files, copyright_lines, private, owner
    ):
        """Merge one artifact occurrence into the per-artifact Java rows."""
        name = f"{art['group']}:{art['artifact']}" if art["group"] else art["artifact"]
        row = rows.get(name)
        if row is None:
            licenses, source, url = self._maven_license(art)
            row = rows[name] = {
                "name": name,
                "version": art["version"],
                "source": (
                    f"https://central.sonatype.com/artifact/{art['group']}/"
                    f"{art['artifact']}/{art['version']}"
                    if art["group"]
                    else ""
                ),
                "license": " | ".join(licenses) if licenses else "Unknown",
                "license_source": source,
                "source_code_url": url,
                "package_provider": "maven" if art["group"] else "jar",
                "package_type": "java",
                "is_dependency": False,
                "parent_packages": [],
                "license_files": [],
                "copyright": [],
                "is_private_env": private,
                "owning_dpkg": owner,
            }
        elif art["version"] not in row["version"].split(", "):
            row["version"] = ", ".join(
                sorted(row["version"].split(", ") + [art["version"]])
            )
        row["is_private_env"] = row["is_private_env"] and private
        row["owning_dpkg"] = row["owning_dpkg"] or owner
        if jar_path not in row["parent_packages"]:
            row["parent_packages"].append(jar_path)
        for field, values in (
            ("license_files", license_files),
            ("copyright", copyright_lines),
        ):
            for value in values:
                if value not in row[field]:
                    row[field].append(value)

    def _maven_license(self, art, max_parents=2):
        """Licenses declared for an artifact, following parent POMs.

        Returns:
            Tuple of (license names, license_source, SCM URL)
        """
        if not art["group"]:
            if art.get("manifest_license"):
                return [art["manifest_license"]], "MANIFEST.MF Bundle-License", ""
            return [], "No Maven metadata", ""
        pom_text = art["pom"]
        source = "embedded pom.xml"
        if not pom_text:
            pom_text = self.metadata_fetcher.get_maven_pom(
                maven_pom_url(art["group"], art["artifact"], art["version"])
            )
            source = "Maven Central pom"
        pom = parse_pom(pom_text)
        url = pom["url"]
        for _ in range(max_parents):
            if pom["licenses"] or not pom["parent"]:
                break
            parent = pom["parent"]
            pom = parse_pom(self.metadata_fetcher.get_maven_pom(maven_pom_url(*parent)))
            source = f"parent pom {':'.join(parent)}"
            url = url or pom["url"]
        if pom["licenses"]:
            return pom["licenses"], source, url
        return [], "Not declared in pom", url

    def _extract_npm_packages(self, container):
        """Extract npm packages installed under node_modules directories.

        Returns:
            List of package dictionaries
        """
        self._log("Extracting npm packages...")
        found = parse_npm_scan(self._exec_text(container, NPM_SCAN_SCRIPT))
        if not found:
            return []
        owners = self._dpkg_owners(container, [p["directory"] for p in found])

        rows = {}
        for pkg in found:
            row = rows.get(pkg["name"])
            if row is None:
                row = rows[pkg["name"]] = {
                    "name": pkg["name"],
                    "version": pkg["version"],
                    "source": f"https://www.npmjs.com/package/{pkg['name']}"
                    f"/v/{pkg['version']}",
                    "license": pkg["license"] or "Unknown",
                    "license_source": "package.json"
                    if pkg["license"]
                    else "Not declared",
                    "source_code_url": pkg["repository"],
                    "package_provider": "npm",
                    "package_type": "npm",
                    "is_dependency": pkg["is_module"],
                    "parent_packages": [],
                    "install_path": [],
                    "license_files": [],
                    "copyright": [],
                    "owning_dpkg": owners.get(pkg["directory"], ""),
                }
            else:
                versions = row["version"].split(", ")
                if pkg["version"] not in versions:
                    row["version"] = ", ".join(sorted(versions + [pkg["version"]]))
                row["is_dependency"] = row["is_dependency"] and pkg["is_module"]
            row["install_path"].append(pkg["directory"])
            for field in ("license_files", "copyright"):
                for value in pkg[field]:
                    if value not in row[field]:
                        row[field].append(value)

        self._log(f"Found {len(rows)} npm packages")
        return sorted(rows.values(), key=lambda r: r["name"])

    def _scan_python_dists(self, container):
        """Run the dist-info filesystem scan inside the container.

        Returns:
            List of parsed distributions, empty if the scan could not run
        """
        try:
            exit_code, output = container.exec_run(
                ["sh", "-c", DIST_SCAN_SCRIPT], demux=False
            )
            if exit_code != 0 or not output:
                return []
            return parse_dist_scan(output.decode("utf-8", errors="ignore"))
        except Exception as e:
            self._log(f"Warning: dist-info scan failed: {e}")
            return []

    def _extract_pip_packages(self, container):
        """Extract Python packages using pip list.

        Args:
            container: Docker container object

        Returns:
            List of Python package dictionaries
        """
        packages = []

        # Try to get pip list output
        try:
            # Try pip list --format=json
            exit_code, output = container.exec_run(
                "pip list --format=json", demux=False
            )

            if exit_code == 0 and output:
                output_str = output.decode("utf-8")
                # Filter out pip notices/warnings - only keep JSON lines
                lines = output_str.split("\n")
                json_line = None
                for line in lines:
                    if line.strip().startswith("["):
                        json_line = line
                        break

                if json_line:
                    pip_packages = json.loads(json_line)
                    self._log(f"Found {len(pip_packages)} Python packages")
                else:
                    self._log("No valid JSON output from pip list")
                    return packages

                for pkg in pip_packages:
                    name = pkg.get("name", "")
                    version = pkg.get("version", "")

                    if name:
                        # Get metadata from PyPI
                        metadata = self.metadata_fetcher.get_pypi_metadata(
                            name, version
                        )
                        packages.append(
                            {
                                "name": name,
                                "version": version,
                                "source": metadata.get(
                                    "source", f"https://pypi.org/project/{name}/"
                                ),
                                "license": metadata.get("license", "Unknown"),
                                "license_source": metadata.get(
                                    "license_source", "PyPI API"
                                ),
                                "source_code_url": metadata.get("source_code_url", ""),
                                "package_provider": "PIP",
                            }
                        )
        except Exception as e:
            self._log(f"Warning: Failed to extract Python packages: {e}")

        return packages

    def _get_python_dependencies(self, container):
        """Get dependency information for Python packages.

        Args:
            container: Docker container object

        Returns:
            Dictionary mapping package names to their dependencies
        """
        self._log("Extracting Python package dependencies...")
        dependencies = {}

        try:
            # Get list of all packages first
            exit_code, output = container.exec_run(
                "pip list --format=json", demux=False
            )

            if exit_code != 0:
                return dependencies

            output_str = output.decode("utf-8")
            lines = output_str.split("\n")
            json_line = None
            for line in lines:
                if line.strip().startswith("["):
                    json_line = line
                    break

            if not json_line:
                return dependencies

            packages = json.loads(json_line)

            # Get dependencies for each package
            for pkg in packages:
                pkg_name = pkg.get("name", "")
                if not pkg_name:
                    continue

                # Use pip show to get dependencies
                exit_code, show_output = container.exec_run(
                    f"pip show {pkg_name}", demux=False
                )

                if exit_code == 0:
                    show_str = show_output.decode("utf-8")
                    for line in show_str.split("\n"):
                        if line.startswith("Requires:"):
                            deps_str = line.split("Requires:")[1].strip()
                            if deps_str and deps_str != "":
                                deps = [d.strip() for d in deps_str.split(",")]
                                dependencies[pkg_name] = deps
                            break

            self._log(f"Found dependencies for {len(dependencies)} packages")
        except Exception as e:
            self._log(f"Warning: Failed to extract dependencies: {e}")

        return dependencies

    def _extract_binary_packages(self, container):
        """Extract binary/system packages from container.

        Args:
            container: Docker container object

        Returns:
            List of binary package dictionaries
        """
        self._log("Extracting binary packages...")
        packages = []

        # Try dpkg (Debian/Ubuntu)
        packages_dpkg = self._extract_dpkg_packages(container)
        if packages_dpkg:
            packages.extend(packages_dpkg)
            return packages

        # Try rpm (RedHat/CentOS)
        packages_rpm = self._extract_rpm_packages(container)
        if packages_rpm:
            packages.extend(packages_rpm)
            return packages

        # Try apk (Alpine)
        packages_apk = self._extract_apk_packages(container)
        if packages_apk:
            packages.extend(packages_apk)

        return packages

    def _extract_dpkg_packages(self, container):
        """Extract packages using dpkg (Debian/Ubuntu)."""
        try:
            exit_code, output = container.exec_run(
                "dpkg-query -W -f='${Package}|${Version}|${Homepage}|${Source}\n'",
                demux=False,
            )

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")
            self._log(f"Found {len(lines)} dpkg packages")

            for line in lines:
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1]
                    homepage = parts[2] if len(parts) > 2 else ""
                    source = parts[3] if len(parts) > 3 else name

                    # Get license info
                    (
                        license_info,
                        license_source,
                        license_files,
                        copyright_lines,
                    ) = self._get_dpkg_license(container, name)

                    packages.append(
                        {
                            "name": name,
                            "version": version,
                            "source": homepage or f"https://packages.ubuntu.com/{name}",
                            "license": license_info,
                            "license_source": license_source,
                            "source_code_url": homepage,
                            "package_provider": "dpkg",
                            "license_files": license_files,
                            "copyright": copyright_lines,
                        }
                    )

            return packages
        except Exception as e:
            self._log(f"dpkg extraction failed: {e}")
            return []

    def _get_dpkg_license(self, container, package_name):
        """Get license information for a dpkg package.

        Uses /usr/share/doc/<package>/copyright when present. Packages that
        install no copyright file (vendor packages such as Nsight or DPDK
        builds) fall back to license-like files the package itself owns.

        Returns:
            Tuple of (license_info, license_source, license_files,
            copyright_lines)
        """
        try:
            copyright_file = f"/usr/share/doc/{package_name}/copyright"
            exit_code, output = container.exec_run(f"cat {copyright_file}", demux=False)
            if exit_code == 0 and output:
                content = output.decode("utf-8", errors="ignore")
                license_info = self._parse_copyright_file(content)
                return (
                    license_info,
                    copyright_file,
                    [copyright_file],
                    extract_copyright_lines(content.splitlines())[:MAX_COPYRIGHT_LINES],
                )
        except Exception as e:
            self._log(f"  Warning: Failed to read {package_name} copyright: {e}")

        license_files = self._dpkg_owned_license_files(container, package_name)
        if license_files:
            result = self._license_from_files(container, license_files)
            if result:
                return result[0], result[1], license_files, []
            return "Unknown", "Not detected in package files", license_files, []
        return "Unknown", "Not found", [], []

    def _dpkg_owned_license_files(self, container, package_name):
        """Non-empty license/EULA/notice files installed by a dpkg package."""
        try:
            exit_code, output = container.exec_run(
                [
                    "sh",
                    "-c",
                    'dpkg-query -L "$1" 2>/dev/null'
                    " | grep -iE '/[^/]*(licen[cs]e|copying|eula|notice)[^/]*$'"
                    ' | while IFS= read -r f; do [ -f "$f" ] && [ -s "$f" ]'
                    ' && printf "%s\\n" "$f"; done',
                    "sh",
                    package_name,
                ],
                demux=False,
            )
            if exit_code != 0 or not output:
                return []
            return output.decode("utf-8", errors="ignore").splitlines()
        except Exception as e:
            self._log(f"  Warning: Failed to list files of {package_name}: {e}")
            return []

    def _link_python_to_dpkg(self, container, packages):
        """Set owning_dpkg on Python dists installed by Debian packages.

        Distro-packaged modules (python3-pip, python3-setuptools, ...) often
        ship no license in their dist-info; their license lives in the owning
        package's copyright file, which is merged into the Python row.

        Args:
            container: Docker container object
            packages: All package dictionaries of the image (modified in place)
        """
        path_to_python = {}
        for pkg in packages:
            if pkg["package_type"] != "python":
                continue
            for path in pkg.get("install_path", []):
                if path.startswith("/usr/lib/"):
                    path_to_python[path] = pkg
        if not path_to_python:
            return

        try:
            _, output = container.exec_run(
                ["dpkg-query", "-S"] + list(path_to_python), demux=False
            )
        except Exception as e:
            self._log(f"Warning: dpkg ownership lookup failed: {e}")
            return

        dpkg_rows = {
            pkg["name"]: pkg for pkg in packages if pkg["package_provider"] == "dpkg"
        }
        for line in (output or b"").decode("utf-8", errors="ignore").splitlines():
            if line.startswith("diversion by "):
                continue
            owners, sep, path = line.partition(": ")
            pkg = path_to_python.get(path.strip())
            if not sep or pkg is None or pkg.get("owning_dpkg"):
                continue
            owner = owners.split(",")[0].strip().split(":")[0]
            pkg["owning_dpkg"] = owner
            dpkg_row = dpkg_rows.get(owner)
            if dpkg_row is None:
                continue
            for field in ("license_files", "copyright"):
                for value in dpkg_row.get(field, []):
                    if value not in pkg[field]:
                        pkg[field].append(value)
            if pkg.get("license") == "Unknown" and dpkg_row["license"] != "Unknown":
                pkg["license"] = dpkg_row["license"]
                pkg["license_source"] = f"dpkg {owner}: {dpkg_row['license_source']}"

    def _parse_copyright_file(self, content):
        """Parse copyright file to extract license information.

        Args:
            content: Copyright file content

        Returns:
            License string (may contain multiple licenses separated by " | ") or "Unknown"
        """
        if not content:
            return "Unknown"

        # Machine-readable (DEP-5) files name each license on a "License:"
        # line; those short names are authoritative
        names = dep5_license_names(content)
        if names:
            return " | ".join(names)

        # Free-form files: only licenses recognised from their text. Prose is
        # never returned as a license name; the file itself is listed in
        # license_files for review.
        detected = self.metadata_fetcher._detect_license_from_content(content)
        if not detected:
            return "Unknown"
        if _DEP5_LICENSE_RE.search(content):
            # License fields hold text that was not identified, so the
            # detected list may be incomplete: keep the package unresolved
            return f"{detected} | Unknown"
        return detected

    def _parse_license_field(self, license_text):
        """Parse a license field that may contain multiple licenses.

        Handles formats like:
        - "MIT or Apache-2.0"
        - "GPL-2.0+"
        - "Apache-2.0 and BSD-3-Clause"
        - "NVIDIA Proprietary"
        - "Custom Corporate License"

        Args:
            license_text: License field text

        Returns:
            List of license identifiers
        """
        if not license_text:
            return []

        licenses = []
        import re

        # Check for corporate/proprietary licenses first
        # These often contain terms like "Proprietary", "Commercial", company names, etc.
        proprietary_indicators = [
            r"\bproprietary\b",
            r"\bcommercial\b",
            r"\bcorporate\b",
            r"\bnvidia\b",
            r"\bcustom\b",
            r"\binternal\b",
        ]

        lower_text = license_text.lower()
        is_proprietary = any(
            re.search(pattern, lower_text) for pattern in proprietary_indicators
        )

        # Try to detect standard licenses in the text
        detected = self.metadata_fetcher._detect_license_from_content(license_text)
        if detected:
            # detected may be multiple licenses joined by " | "
            licenses.extend(detected.split(" | "))

        if is_proprietary:
            # Extract the proprietary license name
            # Try to find the license name (often before a period or newline)
            clean_license = re.sub(r"\s+", " ", license_text.strip())

            # Look for common patterns:
            # "NVIDIA Proprietary License"
            # "Proprietary and confidential"
            # "Commercial License"

            # Try to extract just the license statement (first line or sentence)
            lines = clean_license.split("\n")
            first_line = lines[0].strip()

            # If first line is short and contains proprietary indicator, use it
            if len(first_line) < 100 and any(
                re.search(pattern, first_line.lower())
                for pattern in proprietary_indicators
            ):
                proprietary_license = first_line
            else:
                # Try to extract first sentence
                parts = clean_license.split(".", 1)
                if (
                    parts[0]
                    and len(parts[0]) < 100
                    and any(
                        re.search(pattern, parts[0].lower())
                        for pattern in proprietary_indicators
                    )
                ):
                    proprietary_license = parts[0].strip()
                else:
                    # Fall back to first 100 chars
                    proprietary_license = clean_license[:100].strip()
                    if len(clean_license) > 100:
                        proprietary_license += "..."

            # Add if not already in licenses
            if proprietary_license not in licenses:
                licenses.append(proprietary_license)

        elif not licenses:
            # No standard license detected and not obviously proprietary
            # Return the text as-is (but sanitized and truncated)
            clean_license = re.sub(r"\s+", " ", license_text.strip())

            # Try to extract just the license name
            # Check if it looks like a full license text (contains multiple sentences)
            if "." in clean_license and len(clean_license) > 200:
                # Likely full license text, try to extract name from first sentence
                first_sentence = clean_license.split(".")[0].strip()
                if len(first_sentence) < 100:
                    clean_license = first_sentence
                else:
                    clean_license = clean_license[:100].strip() + "..."
            elif len(clean_license) > 100:
                clean_license = clean_license[:100].strip() + "..."

            licenses.append(clean_license)

        return list(dict.fromkeys(licenses))

    def _enrich_unknown_licenses(self, packages, container):
        """Try to find licenses for packages with unknown licenses.

        Args:
            packages: List of package dictionaries
            container: Docker container object
        """
        self._log("Enriching unknown licenses...")
        enriched_count = 0

        for pkg in packages:
            if pkg.get("license") != "Unknown":
                continue

            license_found = None
            license_source = None

            # Try license files already verified to exist in the image
            if pkg.get("license_files"):
                result = self._license_from_files(container, pkg["license_files"])
                if result:
                    license_found, license_source = result

            # Try to get license from installed package files (for Python packages)
            if not license_found and pkg["package_type"] == "python":
                result = self._get_python_package_license(container, pkg["name"])
                if result:
                    license_found, license_source = result

            # If still unknown, try source code URL
            if not license_found and pkg.get("source_code_url"):
                license_found = self.metadata_fetcher.get_license_from_source_url(
                    pkg["source_code_url"]
                )
                if license_found:
                    license_source = f"Source URL: {pkg['source_code_url']}"

            if license_found:
                pkg["license"] = license_found
                pkg["license_source"] = license_source or "Enriched"
                enriched_count += 1
                self._log(f"  Found license for {pkg['name']}: {license_found}")

        if enriched_count > 0:
            self._log(f"Enriched licenses for {enriched_count} packages")

    def _license_from_files(self, container, license_files):
        """Detect a license from in-image license files.

        Files named like a license are tried before NOTICE/AUTHORS-style files.

        Args:
            container: Docker container object
            license_files: Absolute paths of license-like files in the image

        Returns:
            Tuple of (license_string, license_source) or None
        """
        ordered = sorted(
            license_files,
            key=lambda path: "licen" not in path.rsplit("/", 1)[-1].lower(),
        )
        for path in ordered[:3]:
            try:
                exit_code, output = container.exec_run(["cat", path], demux=False)
            except Exception as e:
                self._log(f"  Warning: Failed to read {path}: {e}")
                continue
            if exit_code != 0 or not output:
                continue
            detected = self.metadata_fetcher._detect_license_from_content(
                output.decode("utf-8", errors="ignore")[:5000]
            )
            if detected:
                return detected, path
        return None

    def _get_python_package_license(self, container, package_name):
        """Try to find license from Python package metadata files.

        Args:
            container: Docker container object
            package_name: Name of the Python package

        Returns:
            Tuple of (license_string, license_source) or None
        """
        try:
            # Try to find package metadata directory
            exit_code, output = container.exec_run(
                f"pip show -f {package_name}", demux=False
            )

            if exit_code != 0:
                return None

            output_str = output.decode("utf-8", errors="ignore")

            # First check if pip show already has license info
            for line in output_str.split("\n"):
                if line.startswith("License:"):
                    license_info = line.split("License:", 1)[1].strip()
                    if license_info and license_info not in ["", "UNKNOWN", "Unknown"]:
                        # Parse the license text to identify the actual license type
                        # Parse multiple licenses if present
                        parsed_licenses = self._parse_license_field(license_info)
                        if parsed_licenses:
                            license_str = " | ".join(parsed_licenses)
                            return license_str, "pip show metadata (parsed)"
                        # Otherwise fall through to read LICENSE files

            # Try to find and read LICENSE files from package
            lines = output_str.split("\n")
            in_files_section = False
            license_files = []

            for line in lines:
                if line.startswith("Files:"):
                    in_files_section = True
                    continue
                if in_files_section and line.strip():
                    # Look for LICENSE files
                    file_path = line.strip()
                    if any(
                        name in file_path.upper()
                        for name in ["LICENSE", "COPYING", "COPYRIGHT"]
                    ):
                        license_files.append(file_path)

            # Try to read LICENSE files
            if license_files:
                # Get Location (installation directory)
                location = None
                for line in lines:
                    if line.startswith("Location:"):
                        location = line.split("Location:", 1)[1].strip()
                        break

                if location:
                    for license_file in license_files[
                        :3
                    ]:  # Check first 3 license files
                        full_path = f"{location}/{license_file}"
                        exit_code, content_output = container.exec_run(
                            f"cat {full_path}", demux=False
                        )
                        if exit_code == 0:
                            content = content_output.decode("utf-8", errors="ignore")
                            # Use the same detection logic as for source URLs
                            license_detected = (
                                self.metadata_fetcher._detect_license_from_content(
                                    content[:5000]
                                )
                            )
                            if license_detected:
                                return license_detected, full_path

        except Exception as e:
            self._log(f"  Warning: Failed to get license for {package_name}: {e}")

        return None

    def _extract_rpm_packages(self, container):
        """Extract packages using rpm (RedHat/CentOS)."""
        try:
            exit_code, output = container.exec_run(
                "rpm -qa --queryformat '%{NAME}|%{VERSION}-%{RELEASE}|%{LICENSE}|%{URL}\n'",
                demux=False,
            )

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")
            self._log(f"Found {len(lines)} rpm packages")

            for line in lines:
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    packages.append(
                        {
                            "name": parts[0],
                            "version": parts[1],
                            "source": parts[3] if len(parts) > 3 else "",
                            "license": parts[2] if len(parts) > 2 else "Unknown",
                            "license_source": "RPM metadata",
                            "source_code_url": parts[3] if len(parts) > 3 else "",
                            "package_provider": "RPM",
                        }
                    )

            return packages
        except Exception as e:
            self._log(f"rpm extraction failed: {e}")
            return []

    def _extract_apk_packages(self, container):
        """Extract packages using apk (Alpine)."""
        try:
            exit_code, output = container.exec_run("apk info -v", demux=False)

            if exit_code != 0:
                return []

            packages = []
            lines = output.decode("utf-8").strip().split("\n")

            for line in lines:
                if not line:
                    continue
                # Skip warning messages
                if line.startswith("WARNING:") or "No such file" in line:
                    continue
                # Format is typically: package-version-release
                parts = line.rsplit("-", 2)
                if len(parts) >= 2:
                    name = parts[0]
                    version = "-".join(parts[1:])
                    packages.append(
                        {
                            "name": name,
                            "version": version,
                            "source": f"https://pkgs.alpinelinux.org/package/edge/main/x86_64/{name}",
                            "license": "Unknown",
                            "license_source": "Not available",
                            "source_code_url": "",
                            "package_provider": "APK",
                        }
                    )

            self._log(f"Found {len(packages)} apk packages")
            return packages
        except Exception as e:
            self._log(f"apk extraction failed: {e}")
            return []
