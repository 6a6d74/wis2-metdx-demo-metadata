import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from wis2node import wnm

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = Draft202012Validator(json.loads((FIXTURES / "wnm-1.3.0-bundled.json").read_text()),
                              format_checker=FormatChecker())


@pytest.mark.parametrize("rel", ["canonical", "update", "deletion"])
def test_messages_validate_against_official_wnm_schema(record, rel):
    data = json.dumps(record).encode()
    deletion = rel == "deletion"
    m = wnm.build(centre_id="io-wis2dev-metdx-demo", metadata_id=record["id"], geometry=record["geometry"],
                  href="https://6a6d74.github.io/x/metadata/a/a.20261030T000000Z.json", rel=rel,
                  datetime_=wnm.record_datetime(record),
                  integrity=None if deletion else wnm.sha512_b64(data), length=None if deletion else len(data))
    assert [e.message for e in SCHEMA.iter_errors(m)] == []
    assert m["properties"]["metadata_id"] == record["id"]
    assert m["properties"]["data_id"] == f"io-wis2dev-metdx-demo/metadata/{record['id']}"
    assert m["links"][0]["rel"] == rel
    assert ("integrity" in m["properties"]) is not deletion


def test_null_datetime_and_geometry_still_valid():
    m = wnm.build(centre_id="c", metadata_id="urn:wmo:md:c:x", geometry=None, href="https://x/y", rel="deletion")
    assert m["geometry"] is None and m["properties"]["datetime"] is None
    assert list(SCHEMA.iter_errors(m)) == []


def test_invalid_rel_rejected():
    with pytest.raises(ValueError):
        wnm.build(centre_id="c", metadata_id="m", geometry=None, href="h", rel="item")


def test_geometry_reduction():
    point = {"type": "Point", "coordinates": [1, 2]}
    assert wnm.wnm_geometry(point) is point
    assert wnm.wnm_geometry(None) is None
    multi = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [1, 0], [1, 1], [0, 0]]], [[[5, 5], [6, 5], [6, 7], [5, 5]]]]}
    assert wnm.wnm_geometry(multi) == {"type": "Polygon",
                                       "coordinates": [[[0, 0], [6, 0], [6, 7], [0, 7], [0, 0]]]}
    assert wnm.wnm_geometry({"type": "MultiPoint", "coordinates": [[3, 4], [3, 4]]}) == \
        {"type": "Point", "coordinates": [3, 4]}
    coll = {"type": "GeometryCollection", "geometries": [point, {"type": "Point", "coordinates": [3, 5]}]}
    assert wnm.wnm_geometry(coll)["type"] == "Polygon"


def test_record_datetime_prefers_updated(record):
    record["properties"]["updated"] = "2026-10-30T01:02:03Z"
    assert wnm.record_datetime(record) == "2026-10-30T01:02:03Z"
    del record["properties"]["updated"]
    assert wnm.record_datetime(record) == record["properties"]["created"]
    assert wnm.record_datetime({}) is None
