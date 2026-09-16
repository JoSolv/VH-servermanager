"""Thunderstore API client and on-disk package index.

Uses the same public v1 endpoint r2modman reads:
``https://thunderstore.io/c/<community>/api/v1/package/``. That returns the
entire community catalogue in one response, so it is fetched rarely and cached
to disk; searching then happens locally and instantly.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

from ..config import THUNDERSTORE_COMMUNITY
from ..util import read_json, write_json

API_BASE = "https://thunderstore.io"
INDEX_URL = f"{API_BASE}/c/{THUNDERSTORE_COMMUNITY}/api/v1/package/"


class ThunderstoreError(RuntimeError):
    pass


def parse_dependency(text: str) -> tuple[str, str, str]:
    """Split a ``namespace-name-1.2.3`` dependency string."""
    head, _, version = text.rpartition("-")
    namespace, _, name = head.partition("-")
    if not namespace or not name or not version:
        raise ThunderstoreError(f"malformed dependency string {text!r}")
    return namespace, name, version


@dataclass(slots=True)
class PackageVersion:
    full_name: str
    name: str
    namespace: str
    version_number: str
    description: str = ""
    icon: str = ""
    download_url: str = ""
    dependencies: list[str] = field(default_factory=list)
    file_size: int = 0
    downloads: int = 0
    website_url: str = ""

    @property
    def package_full_name(self) -> str:
        return f"{self.namespace}-{self.name}"

    @classmethod
    def from_api(cls, payload: dict[str, Any], namespace: str) -> "PackageVersion":
        full_name = payload.get("full_name", "")
        name = payload.get("name", "")
        version = payload.get("version_number", "")
        return cls(
            full_name=full_name or f"{namespace}-{name}-{version}",
            name=name,
            namespace=namespace,
            version_number=version,
            description=payload.get("description", "") or "",
            icon=payload.get("icon", "") or "",
            download_url=payload.get("download_url", "")
            or f"{API_BASE}/package/download/{namespace}/{name}/{version}/",
            dependencies=list(payload.get("dependencies") or []),
            file_size=int(payload.get("file_size") or 0),
            downloads=int(payload.get("downloads") or 0),
            website_url=payload.get("website_url", "") or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "name": self.name,
            "namespace": self.namespace,
            "version_number": self.version_number,
            "description": self.description,
            "icon": self.icon,
            "download_url": self.download_url,
            "dependencies": self.dependencies,
            "file_size": self.file_size,
            "downloads": self.downloads,
            "website_url": self.website_url,
        }


@dataclass(slots=True)
class Package:
    full_name: str
    name: str
    owner: str
    package_url: str = ""
    is_deprecated: bool = False
    is_pinned: bool = False
    has_nsfw_content: bool = False
    categories: list[str] = field(default_factory=list)
    rating_score: int = 0
    total_downloads: int = 0
    versions: list[PackageVersion] = field(default_factory=list)

    @property
    def latest(self) -> PackageVersion | None:
        return self.versions[0] if self.versions else None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Package":
        owner = payload.get("owner", "")
        versions = [
            PackageVersion.from_api(v, owner) for v in payload.get("versions", [])
        ]
        return cls(
            full_name=payload.get("full_name", ""),
            name=payload.get("name", ""),
            owner=owner,
            package_url=payload.get("package_url", "") or "",
            is_deprecated=bool(payload.get("is_deprecated")),
            is_pinned=bool(payload.get("is_pinned")),
            has_nsfw_content=bool(payload.get("has_nsfw_content")),
            categories=list(payload.get("categories") or []),
            rating_score=int(payload.get("rating_score") or 0),
            total_downloads=sum(v.downloads for v in versions),
            versions=versions,
        )

    def to_dict(self, *, with_versions: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "full_name": self.full_name,
            "name": self.name,
            "owner": self.owner,
            "package_url": self.package_url,
            "is_deprecated": self.is_deprecated,
            "is_pinned": self.is_pinned,
            "categories": self.categories,
            "rating_score": self.rating_score,
            "total_downloads": self.total_downloads,
            "latest_version": self.latest.version_number if self.latest else "",
            "description": self.latest.description if self.latest else "",
            "icon": self.latest.icon if self.latest else "",
        }
        if with_versions:
            payload["versions"] = [v.to_dict() for v in self.versions]
        return payload


class ThunderstoreIndex:
    """In-memory catalogue backed by a disk cache."""

    def __init__(self, cache_dir: Path, ttl: float = 3600.0) -> None:
        self._cache_file = cache_dir / "thunderstore_index.json"
        self._ttl = ttl
        self._packages: dict[str, Package] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    @property
    def loaded(self) -> bool:
        return bool(self._packages)

    @property
    def age(self) -> float:
        return time.time() - self._fetched_at if self._fetched_at else float("inf")

    @property
    def count(self) -> int:
        return len(self._packages)

    def _ingest(self, payload: Iterable[dict[str, Any]]) -> None:
        packages: dict[str, Package] = {}
        for item in payload:
            try:
                package = Package.from_api(item)
            except (TypeError, ValueError):
                continue
            if package.full_name:
                packages[package.full_name.lower()] = package
        self._packages = packages

    async def ensure(self, force: bool = False) -> None:
        """Load the catalogue, from disk or from the network as needed."""
        async with self._lock:
            if not force and self._packages and self.age < self._ttl:
                return

            if not force and not self._packages:
                cached = read_json(self._cache_file)
                if isinstance(cached, dict) and cached.get("packages"):
                    self._ingest(cached["packages"])
                    self._fetched_at = float(cached.get("fetched_at", 0))
                    if self.age < self._ttl:
                        return

            try:
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    response = await client.get(
                        INDEX_URL, headers={"Accept": "application/json"}
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._packages:
                    # Keep serving the stale catalogue rather than breaking the page.
                    return
                raise ThunderstoreError(
                    f"could not reach Thunderstore: {exc}"
                ) from exc

            if not isinstance(payload, list):
                raise ThunderstoreError("unexpected index payload")
            self._ingest(payload)
            self._fetched_at = time.time()
            write_json(self._cache_file, {"fetched_at": self._fetched_at, "packages": payload})

    # ------------------------------------------------------------------ #
    def get(self, package_full_name: str) -> Package | None:
        return self._packages.get(package_full_name.lower())

    def version(self, namespace: str, name: str, version: str) -> PackageVersion | None:
        package = self.get(f"{namespace}-{name}")
        if package is None:
            return None
        for candidate in package.versions:
            if candidate.version_number == version:
                return candidate
        return None

    def resolve_dependency(self, text: str) -> PackageVersion | None:
        """Find the exact version named by a dependency string.

        Falls back to the latest version when that exact build has been
        removed from Thunderstore, which happens with deprecated packages.
        """
        namespace, name, version = parse_dependency(text)
        exact = self.version(namespace, name, version)
        if exact is not None:
            return exact
        package = self.get(f"{namespace}-{name}")
        return package.latest if package else None

    def search(
        self,
        query: str = "",
        *,
        limit: int = 50,
        offset: int = 0,
        include_deprecated: bool = False,
        include_nsfw: bool = False,
        category: str = "",
    ) -> tuple[list[Package], int]:
        terms = [t for t in query.lower().split() if t]
        results: list[tuple[int, Package]] = []

        for package in self._packages.values():
            if package.is_deprecated and not include_deprecated:
                continue
            if package.has_nsfw_content and not include_nsfw:
                continue
            if category and category not in package.categories:
                continue

            if terms:
                haystack = " ".join(
                    (
                        package.name.lower(),
                        package.owner.lower(),
                        (package.latest.description.lower() if package.latest else ""),
                    )
                )
                if not all(term in haystack for term in terms):
                    continue
                # Rank exact and prefix matches on the package name first.
                name = package.name.lower()
                score = 0
                if name == query.lower():
                    score = 3
                elif any(name.startswith(term) for term in terms):
                    score = 2
                elif all(term in name for term in terms):
                    score = 1
            else:
                score = 0
            results.append((score, package))

        results.sort(key=lambda pair: (-pair[0], -pair[1].rating_score, pair[1].name.lower()))
        total = len(results)
        window = results[offset: offset + limit]
        return [package for _, package in window], total

    def categories(self) -> list[str]:
        seen: set[str] = set()
        for package in self._packages.values():
            seen.update(package.categories)
        return sorted(seen)
