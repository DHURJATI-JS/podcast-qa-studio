"""Static assets: content-hashed URLs for cache busting + long-lived caching of versioned files.

The CSS (Tailwind) and JS (esbuild) bundles in ``static/dist`` are minified at build time.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).resolve().parent / "static"
DIST_DIR = STATIC_DIR / "dist"
_versions: dict[str, str] = {}


def asset_url(name: str) -> str:
    version = _versions.get(name)
    if version is None:
        path = (DIST_DIR / name).resolve()
        if DIST_DIR.resolve() in path.parents and path.is_file():
            version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        else:
            version = "missing"
        _versions[name] = version
    return f"/static/dist/{name}?v={version}"


def reset_versions() -> None:
    _versions.clear()


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            versioned = b"v=" in scope.get("query_string", b"")
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "public, max-age=3600"
        return response
