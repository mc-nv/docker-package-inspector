"""Package metadata fetching from PyPI and other sources."""

import re
import sys
from typing import Dict, Optional

import requests


class PackageMetadataFetcher:
    """Fetches package metadata from various sources."""

    def __init__(self, timeout=10):
        """Initialize the metadata fetcher.

        Args:
            timeout: HTTP request timeout in seconds
        """
        self.timeout = timeout
        self.session = requests.Session()
        self.cache = {}

    def get_pypi_metadata(
        self, package_name: str, version: Optional[str] = None
    ) -> Dict:
        """Fetch metadata for a Python package from PyPI.

        Args:
            package_name: Name of the package
            version: Optional specific version

        Returns:
            Dictionary with package metadata
        """
        cache_key = f"{package_name}:{version}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        metadata = {
            "source": f"https://pypi.org/project/{package_name}/",
            "license": "Unknown",
            "license_source": "PyPI API",
            "source_code_url": "",
        }

        try:
            # Query PyPI JSON API
            if version:
                url = f"https://pypi.org/pypi/{package_name}/{version}/json"
            else:
                url = f"https://pypi.org/pypi/{package_name}/json"

            response = self.session.get(url, timeout=self.timeout)

            if response.status_code == 200:
                data = response.json()
                info = data.get("info", {})

                # Extract license
                license_info = info.get("license")
                if license_info:
                    # Parse the license text to identify the actual license type
                    detected_license = self._detect_license_from_content(license_info)
                    if detected_license:
                        metadata["license"] = detected_license
                        metadata["license_source"] = "PyPI API (license field, parsed)"
                    else:
                        # If detection fails, use the raw license text
                        metadata["license"] = license_info
                        metadata["license_source"] = "PyPI API (license field)"
                else:
                    # Try classifiers
                    classifiers = info.get("classifiers", [])
                    for classifier in classifiers:
                        if classifier.startswith("License ::"):
                            metadata["license"] = classifier.split("::")[-1].strip()
                            metadata["license_source"] = "PyPI API (classifier)"
                            break

                # Extract source code URL
                project_urls = info.get("project_urls", {})

                # Try common source code URL keys
                for key in [
                    "Source",
                    "Source Code",
                    "Repository",
                    "Homepage",
                    "GitHub",
                ]:
                    if key in project_urls:
                        metadata["source_code_url"] = project_urls[key]
                        break

                # Fallback to home_page or package_url
                if not metadata["source_code_url"]:
                    metadata["source_code_url"] = info.get("home_page") or info.get(
                        "package_url", ""
                    )

        except Exception as e:
            # Silently fail and return default metadata
            pass

        self.cache[cache_key] = metadata
        return metadata

    def get_license_from_source_url(self, source_url: str) -> Optional[str]:
        """Fetch license information from source code repository URL.

        Args:
            source_url: URL to source code repository (GitHub, GitLab, etc.)

        Returns:
            License name or None if not found
        """
        if not source_url or source_url == "Unknown":
            return None

        cache_key = f"license:{source_url}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        license_info = None

        try:
            # GitHub API
            if "github.com" in source_url:
                license_info = self._get_github_license(source_url)

            # GitLab API
            elif "gitlab" in source_url:
                license_info = self._get_gitlab_license(source_url)

            # Fallback: Try to fetch LICENSE file directly
            if not license_info:
                license_info = self._fetch_license_file(source_url)

        except Exception:
            pass

        self.cache[cache_key] = license_info
        return license_info

    def _get_github_license(self, github_url: str) -> Optional[str]:
        """Get license from GitHub repository using API.

        Args:
            github_url: GitHub repository URL

        Returns:
            License name or None
        """
        try:
            # Extract owner/repo from URL
            # Format: https://github.com/owner/repo or https://github.com/owner/repo/...
            match = re.search(r"github\.com/([^/]+)/([^/]+)", github_url)
            if not match:
                return None

            owner, repo = match.groups()
            # Remove .git suffix if present
            repo = repo.replace(".git", "")

            # Use GitHub API to get license
            api_url = f"https://api.github.com/repos/{owner}/{repo}/license"
            response = self.session.get(
                api_url,
                timeout=self.timeout,
                headers={"Accept": "application/vnd.github.v3+json"},
            )

            if response.status_code == 200:
                data = response.json()
                license_obj = data.get("license", {})
                license_name = license_obj.get("spdx_id") or license_obj.get("name")
                if license_name and license_name != "NOASSERTION":
                    return license_name

        except Exception:
            pass

        return None

    def _get_gitlab_license(self, gitlab_url: str) -> Optional[str]:
        """Get license from GitLab repository.

        Args:
            gitlab_url: GitLab repository URL

        Returns:
            License name or None
        """
        try:
            # Try to extract project path and construct API URL
            # Format: https://gitlab.com/owner/repo or https://gitlab.example.com/owner/repo
            match = re.search(r"(gitlab[^/]*)/(.+?)(?:\.git)?(?:/|$)", gitlab_url)
            if not match:
                return None

            gitlab_host = match.group(1)
            project_path = match.group(2).rstrip("/")

            # URL encode the project path
            import urllib.parse

            encoded_path = urllib.parse.quote(project_path, safe="")

            # Try GitLab API
            api_url = f"https://{gitlab_host}/api/v4/projects/{encoded_path}"
            response = self.session.get(api_url, timeout=self.timeout)

            if response.status_code == 200:
                data = response.json()
                # GitLab doesn't have a direct license field, try to get LICENSE file
                license_url = data.get("license_url")
                if license_url:
                    return self._fetch_license_file(license_url)

        except Exception:
            pass

        return None

    def _fetch_license_file(self, base_url: str) -> Optional[str]:
        """Try to fetch and parse LICENSE file from repository.

        Args:
            base_url: Base URL of repository

        Returns:
            Detected license name or None
        """
        try:
            # Common license file names
            license_files = [
                "LICENSE",
                "LICENSE.txt",
                "LICENSE.md",
                "COPYING",
                "COPYING.txt",
            ]

            # Try raw content URLs for GitHub
            if "github.com" in base_url:
                match = re.search(r"github\.com/([^/]+)/([^/]+)", base_url)
                if match:
                    owner, repo = match.groups()
                    repo = repo.replace(".git", "")

                    for license_file in license_files:
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{license_file}"
                        content = self._try_fetch_url(raw_url)
                        if content:
                            return self._detect_license_from_content(content)

                        # Try master branch
                        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/{license_file}"
                        content = self._try_fetch_url(raw_url)
                        if content:
                            return self._detect_license_from_content(content)

        except Exception:
            pass

        return None

    def _try_fetch_url(self, url: str) -> Optional[str]:
        """Try to fetch content from URL.

        Args:
            url: URL to fetch

        Returns:
            Content as string or None
        """
        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return response.text[:5000]  # Limit to first 5KB
        except Exception:
            pass
        return None

    def _detect_license_from_content(self, content: str) -> Optional[str]:
        """Detect license type from license file content.

        Args:
            content: License file content

        Returns:
            License identifier or None
        """
        if not content:
            return None

        content_lower = content.lower()

        # License detection patterns (ordered by specificity)
        license_patterns = [
            (r"apache license\s*,?\s*version 2\.0", "Apache-2.0"),
            (r"apache license version 2", "Apache-2.0"),
            (r"apache-2\.0", "Apache-2.0"),
            (r"gnu general public license.*version 3", "GPL-3.0"),
            (r"gnu general public license.*version 2", "GPL-2.0"),
            (r"gpl-3", "GPL-3.0"),
            (r"gpl-2", "GPL-2.0"),
            (r"gnu lesser general public license.*version 3", "LGPL-3.0"),
            (r"gnu lesser general public license.*version 2", "LGPL-2.0"),
            (r"lgpl-3", "LGPL-3.0"),
            (r"lgpl-2", "LGPL-2.0"),
            (r"mit license", "MIT"),
            (r"permission is hereby granted, free of charge", "MIT"),
            # BSD-3-Clause patterns (ordered by specificity)
            (r"bsd[- ]3[- ]clause", "BSD-3-Clause"),
            (r"3[- ]clause bsd", "BSD-3-Clause"),
            # Key phrase that appears in BSD-3-Clause but not BSD-2-Clause
            (r"neither the name of.*nor the names of its contributors", "BSD-3-Clause"),
            # BSD-3-Clause with numbered clauses
            (
                r"redistribution and use in source and binary forms.*?\n.*?1\..*?\n.*?2\..*?\n.*?3\.",
                "BSD-3-Clause",
            ),
            # General BSD pattern with "redistribution and use" - check for 3-clause indicator
            (
                r"redistribution and use in source and binary forms.*neither the name",
                "BSD-3-Clause",
            ),
            # BSD-2-Clause patterns
            (r"bsd[- ]2[- ]clause", "BSD-2-Clause"),
            (r"2[- ]clause bsd", "BSD-2-Clause"),
            # Generic BSD (if no specific clause count found)
            (r"redistribution and use in source and binary forms", "BSD"),
            (r"mozilla public license.*version 2\.0", "MPL-2.0"),
            (r"mpl-2\.0", "MPL-2.0"),
            (r"isc license", "ISC"),
        ]

        for pattern, license_id in license_patterns:
            if re.search(pattern, content_lower):
                return license_id

        return None
