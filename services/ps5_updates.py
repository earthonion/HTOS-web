"""
Thin wrapper around the `ps5-updates` library so the route layer can stay
async-friendly. The library uses synchronous `requests` under the hood;
we run it via asyncio.to_thread so the event loop isn't blocked.
"""

import asyncio
import logging
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Sony delivery hosts the library hits. URL validation in the route layer
# refuses anything else (defense against SSRF). Listed here for one-place
# update; `VALID_URL_HOSTS` in routes/tools.py is the import target.
PS5_UPDATE_HOSTS = {
    "sgst.prod.dl.playstation.net",
    "gst.prod.dl.playstation.net",
    "ares.dl.playstation.net",
    "zeus.dl.playstation.net",
}

_VERSION_XML_RE = re.compile(r"-version\.xml$", re.I)
_SC_PKG_RE     = re.compile(r"_sc\.pkg$", re.I)



def _urlstr(u):
    """Library sometimes returns a urllib.parse.ParseResult; flatten to str."""
    if u is None: return None
    if hasattr(u, "geturl"):
        return u.geturl()
    return str(u)

def _serialize_pkg(pkg) -> Dict[str, Any]:
    """Turn a ps5_updates.pkg.PKG into a JSON-safe dict."""
    p = pkg.param if pkg and pkg.param else None
    if p is None:
        return {}
    return {
        "name": getattr(p, "name", None),
        "title_id": getattr(p, "title_id", None),
        "content_id": getattr(p, "content_id", None),
        "content_version": getattr(p, "content_version", None),
        "creation_date": str(getattr(p, "creation_date", "") or "") or None,
        "required_system_version": getattr(p, "required_system_version", None),
        "version_url": getattr(p, "version_url", None),
        "supports_120hz": getattr(pkg, "supports_120hz", None),
        "supports_pssr": getattr(pkg, "supports_pssr", None),
        "supports_vrr": getattr(pkg, "supports_vrr", None),
        "supports_hdr": getattr(pkg, "supports_hdr", None),
        "supports_power_saver": getattr(pkg, "supports_power_saver", None),
    }


def _serialize_package(content_package) -> Dict[str, Any]:
    if content_package is None:
        return None
    return {
        "version": getattr(content_package, "version", None),
        "system_version": getattr(content_package, "system_version", None),
        "update_size": getattr(content_package, "update_size", None),
        "manifest_url": getattr(content_package, "manifest_url", None),
        "pkg_url": _urlstr(getattr(getattr(content_package, "pkg", None), "url", None)),
        "pkg": _serialize_pkg(getattr(content_package, "pkg", None)),
    }


def _lookup_sync(url: str) -> Dict[str, Any]:
    """Run the synchronous lookup. Called inside asyncio.to_thread."""
    # Suppress the InsecureRequestWarning the lib emits on every PS5 host.
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    from ps5_updates import title as ps5up
    from ps5_updates import pkg as ps5pkg

    if _SC_PKG_RE.search(url):
        # Direct _sc.pkg URL → PKG object only (no title-level multi-package list)
        p = ps5pkg.PKG.from_url(url)
        return {
            "mode": "pkg",
            "title_id": p.param.title_id if p.param else None,
            "name": p.param.name if p.param else None,
            "latest": {
                "version": p.param.content_version if p.param else None,
                "system_version": p.param.required_system_version if p.param else None,
                "update_size": None,
                "pkg_url": _urlstr(url),
                "pkg": _serialize_pkg(p),
            },
            "packages": [],
            "additional_content": [],
        }

    # version.xml URL → full title with packages + AdditionalContent
    title = ps5up.Ps5TitleUpdate.from_url(url)
    title.get_update()

    additional = []
    for ac in getattr(title, "additional_content", []) or []:
        additional.append({
            "title_id": getattr(ac, "title_id", None),
            "content_id": getattr(ac, "content_id", None),
            "name": getattr(ac, "name", None),
            "latest": _serialize_package(getattr(ac, "latest", None)),
            "packages": [_serialize_package(p) for p in (getattr(ac, "packages", []) or [])],
        })

    return {
        "mode": "title",
        "title_id": title.title_id,
        "content_id": title.content_id,
        "latest": _serialize_package(title.latest),
        "packages": [_serialize_package(p) for p in (title.packages or [])],
        "additional_content": additional,
    }


async def lookup(url: str) -> Dict[str, Any]:
    """Look up PS5 update info for a version XML or _sc.pkg URL."""
    return await asyncio.to_thread(_lookup_sync, url)
