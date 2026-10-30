"""Error code lookup — loads normalized PS4+PS5 error data at import.

Data source: /opt/htos/data/error_codes.json produced from the community
error_codes_ps4.csv (4-line records) + error_codesps5.csv (CSV). ~27k entries,
loaded once into memory; linear scan is trivially fast at this size.
"""
import json
import logging
import os

log = logging.getLogger("error_codes")

_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "error_codes.json")
_ALL: list[dict] = []


def _load() -> list[dict]:
    global _ALL
    if _ALL:
        return _ALL
    try:
        with open(_DATA_PATH) as f:
            _ALL = json.load(f)
        log.info("loaded %d error codes from %s", len(_ALL), _DATA_PATH)
    except Exception as e:
        log.error("failed to load %s: %s", _DATA_PATH, e)
        _ALL = []
    return _ALL


def total() -> int:
    return len(_load())


def total_by_platform() -> dict:
    all_ = _load()
    ps4 = sum(1 for r in all_ if r.get("platform") == "ps4")
    return {"ps4": ps4, "ps5": len(all_) - ps4}


def search(q: str, platform: str | None = None, limit: int = 50) -> list[dict]:
    """Case-insensitive substring match across code / hex / sce / desc.
    Prefix matches ranked first. `platform` in {'ps4','ps5', None}."""
    q = (q or "").strip().lower()
    if not q:
        return []
    all_ = _load()
    exact, prefix, sub = [], [], []
    for r in all_:
        if platform and r.get("platform") != platform:
            continue
        code = r.get("code", "").lower()
        hex_ = r.get("hex", "").lower()
        sce  = r.get("sce", "").lower()
        desc = r.get("desc", "").lower()
        if q == code or q == hex_:
            exact.append(r)
        elif code.startswith(q) or hex_.startswith(q) or sce.startswith(q):
            prefix.append(r)
        elif q in code or q in hex_ or q in sce or q in desc:
            sub.append(r)
        if len(exact) + len(prefix) + len(sub) >= limit * 3:
            # early-out — we have enough candidates to fill `limit` even
            # if all are in the lowest priority bucket
            break
    return (exact + prefix + sub)[:limit]
