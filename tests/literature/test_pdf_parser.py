from __future__ import annotations

import pytest

from neuroagent.literature.models import LiteratureParseError
from neuroagent.literature.pdf_parser import PypdfParser
from tests.literature.pdf_factory import make_blank_pdf, make_text_pdf


def test_pdf_parser_extracts_page_text_metadata_and_doi() -> None:
    content = make_text_pdf(
        (
            "A Resting State Study\nAbstract\nDOI 10.1234/RSFMRI.2024.01",
            "2 Methods\nThe second physical page.",
        ),
        title="A Resting State Study",
        author="Ada Researcher; Lin Scientist",
    )

    parsed = PypdfParser().parse(content)

    assert [page.page_number for page in parsed.pages] == [1, 2]
    assert "second physical page" in parsed.pages[1].text
    assert parsed.metadata.title == "A Resting State Study"
    assert parsed.metadata.authors == ("Ada Researcher", "Lin Scientist")
    assert parsed.metadata.year is None  # PDF creation date is not publication year.
    assert parsed.metadata.doi == "10.1234/RSFMRI.2024.01"


@pytest.mark.parametrize("content", [b"not a pdf", b"%PDF-1.4\ncorrupted"])
def test_pdf_parser_rejects_invalid_pdf(content: bytes) -> None:
    with pytest.raises(LiteratureParseError) as raised:
        PypdfParser().parse(content)
    assert raised.value.code in {"unsupported_file_type", "pdf_parse_failed"}


def test_pdf_parser_rejects_pdf_without_text_layer() -> None:
    with pytest.raises(LiteratureParseError) as raised:
        PypdfParser().parse(make_blank_pdf())
    assert raised.value.code == "pdf_text_layer_missing"
