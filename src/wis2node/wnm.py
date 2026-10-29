"""Build WIS2 Notification Messages (WNM 1.x) for discovery metadata records."""
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone

WNM_CONFORMANCE = "http://wis.wmo.int/spec/wnm/1/conf/core"
METADATA_MEDIA_TYPE = "application/geo+json"

CANONICAL, UPDATE, DELETION = "canonical", "update", "deletion"


def sha512_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positions(coords):
    """Yield every [lon, lat, ...] position in a GeoJSON coordinates array."""
    if coords and isinstance(coords[0], (int, float)):
        yield coords
        return
    for c in coords or []:
        yield from _positions(c)


def wnm_geometry(geometry: dict | None) -> dict | None:
    """WNM allows only Point, Polygon or null: reduce anything else to its bounding box."""
    if not geometry:
        return None
    if geometry.get("type") in ("Point", "Polygon"):
        return geometry
    if geometry.get("type") == "GeometryCollection":
        positions = [p for g in geometry.get("geometries", []) for p in _positions(g.get("coordinates"))]
    else:
        positions = list(_positions(geometry.get("coordinates")))
    if not positions:
        return None
    lons = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    w, e, s, n = min(lons), max(lons), min(lats), max(lats)
    if w == e and s == n:
        return {"type": "Point", "coordinates": [w, s]}
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def record_datetime(record: dict | None) -> str | None:
    """The record's own last-updated (else created) time: the WNM `datetime` for metadata."""
    props = (record or {}).get("properties") or {}
    value = props.get("updated") or props.get("created")
    return value if isinstance(value, str) else None


def build(*, centre_id: str, metadata_id: str, geometry: dict | None, href: str, rel: str,
          datetime_: str | None = None, integrity: str | None = None, length: int | None = None,
          pubtime: str | None = None, message_id: str | None = None) -> dict:
    """A WNM announcing a new (canonical), updated or deleted metadata record.

    WNM requires `datetime` (or start/end) but allows null; for metadata it is the
    record's updated/created time (see `record_datetime`).
    """
    if rel not in (CANONICAL, UPDATE, DELETION):
        raise ValueError(f"invalid rel {rel!r}")
    properties = {
        "data_id": f"{centre_id}/metadata/{metadata_id}",
        "metadata_id": metadata_id,
        "pubtime": pubtime or utc_now(),
        "datetime": datetime_,
    }
    if integrity:
        properties["integrity"] = {"method": "sha512", "value": integrity}
    link = {"href": href, "rel": rel, "type": METADATA_MEDIA_TYPE}
    if length is not None:
        link["length"] = length
    return {
        "id": message_id or str(uuid.uuid4()),
        "conformsTo": [WNM_CONFORMANCE],
        "type": "Feature",
        "geometry": wnm_geometry(geometry),
        "properties": properties,
        "links": [link],
    }
