import json

import pytest

from mathpix_pipeline.config import load_options


def test_rejects_always_outputs_as_conversion_formats(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"conversion_formats": {"mmd": True}}))
    with pytest.raises(ValueError, match="Always-produced"):
        load_options(path)


def test_default_options_are_independent_copies():
    first = load_options(None)
    first["conversion_formats"]["pdf"] = False
    assert load_options(None)["conversion_formats"]["pdf"] is True
