import pytest

from mathpix_pipeline.cli import parser
from mathpix_pipeline.pdf_tools import parse_pages


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1", [1]),
        ("1,3-5,last", [1, 3, 4, 5, 10]),
        ("3,3,2", [3, 2]),
        ("last-last", [10]),
    ],
)
def test_parse_pages(spec, expected):
    assert parse_pages(spec, 10) == expected


@pytest.mark.parametrize("spec", ["", "0", "11", "5-2", "a", "1-"])
def test_parse_pages_errors(spec):
    with pytest.raises(ValueError):
        parse_pages(spec, 10)


def test_html_command_parser_defaults():
    args = parser().parse_args(["html", "run-directory"])
    assert args.dpi == 144
    assert args.quality == 88
    assert args.folder is False
    assert args.no_zip is False
