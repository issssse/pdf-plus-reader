import pytest

from mathpix_pipeline.tex_tools import tune_tex_source


SOURCE = r"""\documentclass[10pt]{article}
\usepackage[swedish]{babel}
\usepackage[version=4]{mhchem}
\begin{document}
bad token
\end{document}
"""


def test_tune_tex_source_is_explicit_and_validated():
    tuned = tune_tex_source(
        SOURCE,
        document_class="extarticle",
        font_size="14pt",
        paper="a4paper",
        margin="18mm",
        babel_language="english",
        omit_packages=["mhchem"],
        replacements=[{"old": "bad token", "new": "good token"}],
    )
    assert r"\documentclass[14pt,a4paper]{extarticle}" in tuned
    assert r"\usepackage[a4paper,margin=18mm]{geometry}" in tuned
    assert r"\usepackage[english]{babel}" in tuned
    assert "% omitted by mathpix-pdf tex-tune" in tuned
    assert "good token" in tuned


def test_tune_tex_rejects_stale_replacement():
    with pytest.raises(ValueError, match="found 0"):
        tune_tex_source(
            SOURCE,
            document_class="article",
            font_size="10pt",
            paper="a4paper",
            margin="18mm",
            replacements=[{"old": "not present", "new": "x"}],
        )
