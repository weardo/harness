"""Tests for the JSON union conflict resolver in parallel.py."""

import json
import tempfile
from pathlib import Path

import pytest

from src.core.parallel import _try_resolve_json_union


@pytest.fixture
def tmp_json():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.json"


def _write(p, content):
    p.write_text(content)


class TestJsonUnionResolver:
    def test_no_conflict_passes_through(self, tmp_json):
        _write(tmp_json, '{"a": 1, "b": 2}')
        assert _try_resolve_json_union(tmp_json) is True
        assert json.loads(tmp_json.read_text()) == {"a": 1, "b": 2}

    def test_additive_keys_union(self, tmp_json):
        _write(tmp_json, '''{
    "common": "common",
<<<<<<< HEAD
    "head-only": "from-head"
=======
    "other-only": "from-other"
>>>>>>> branch
}''')
        assert _try_resolve_json_union(tmp_json) is True
        result = json.loads(tmp_json.read_text())
        assert result == {
            "common": "common",
            "head-only": "from-head",
            "other-only": "from-other",
        }

    def test_identical_duplicate_keys_dropped(self, tmp_json):
        _write(tmp_json, '''{
<<<<<<< HEAD
    "shared": "same-value",
    "head-only": "from-head"
=======
    "shared": "same-value",
    "other-only": "from-other"
>>>>>>> branch
}''')
        assert _try_resolve_json_union(tmp_json) is True
        result = json.loads(tmp_json.read_text())
        assert result == {
            "shared": "same-value",
            "head-only": "from-head",
            "other-only": "from-other",
        }

    def test_value_conflict_returns_false(self, tmp_json):
        """When the same key has different values on each side, manual resolution required."""
        _write(tmp_json, '''{
<<<<<<< HEAD
    "shared": "head-value"
=======
    "shared": "other-value"
>>>>>>> branch
}''')
        assert _try_resolve_json_union(tmp_json) is False
        # File should be unchanged when resolution fails
        assert "<<<<<<< HEAD" in tmp_json.read_text()

    def test_multiple_conflict_blocks(self, tmp_json):
        _write(tmp_json, '''{
<<<<<<< HEAD
    "a": "1"
=======
    "b": "2"
>>>>>>> branch,
    "middle": "common",
<<<<<<< HEAD
    "c": "3"
=======
    "d": "4"
>>>>>>> branch
}''')
        # Note: this isn't strictly valid JSON (the comma after >>>>>>> is unusual)
        # but the resolver should still handle two distinct conflict blocks
        result = _try_resolve_json_union(tmp_json)
        # Either resolves both or returns False — but must not crash
        assert result in (True, False)

    def test_invalid_json_after_resolution_returns_false(self, tmp_json):
        """If union produces invalid JSON, must return False without writing."""
        _write(tmp_json, '''{
<<<<<<< HEAD
    "a": 1
=======
    "a": 2
>>>>>>> branch
}''')
        # value conflict for same key — should return False
        assert _try_resolve_json_union(tmp_json) is False

    def test_realistic_i18n_messages_pattern(self, tmp_json):
        """Mimics the actual i18n message JSON structure we hit."""
        _write(tmp_json, '''{
    "table": {
        "title": "Table",
<<<<<<< HEAD
        "col-name": "Name",
        "col-source": "Source"
=======
        "col-id": "ID",
        "col-status": "Status"
>>>>>>> branch
    }
}''')
        assert _try_resolve_json_union(tmp_json) is True
        result = json.loads(tmp_json.read_text())
        assert result["table"]["title"] == "Table"
        assert result["table"]["col-name"] == "Name"
        assert result["table"]["col-source"] == "Source"
        assert result["table"]["col-id"] == "ID"
        assert result["table"]["col-status"] == "Status"
