from __future__ import annotations

import pytest

from neuroagent.literature.chunker import ScientificChunker
from neuroagent.literature.models import PaperSection, ParagraphSpan


def section(
    section_id: str,
    name: str,
    paragraphs: tuple[ParagraphSpan, ...],
    *,
    subsection: str | None = None,
) -> PaperSection:
    return PaperSection(
        section_id=section_id,
        paper_id="paper-1",
        title=subsection or name,
        section=name,
        subsection=subsection,
        level=2 if subsection else 1,
        page_start=min(item.page_start for item in paragraphs),
        page_end=max(item.page_end for item in paragraphs),
        text="\n\n".join(item.text for item in paragraphs),
        paragraphs=paragraphs,
    )


def test_chunker_is_section_aware_and_preserves_page_traceability() -> None:
    methods = section(
        "section-methods",
        "Methods",
        (
            ParagraphSpan(
                text="Participants rested quietly. Images were acquired on a scanner.",
                page_start=5,
                page_end=5,
            ),
            ParagraphSpan(
                text="Motion was corrected. Signals were filtered. Nuisance terms were regressed.",
                page_start=6,
                page_end=6,
            ),
        ),
        subsection="Preprocessing",
    )
    results = section(
        "section-results",
        "Results",
        (ParagraphSpan(text="ALFF differed between groups.", page_start=8, page_end=8),),
    )

    chunks = ScientificChunker(target_tokens=12, overlap_tokens=3).chunk(
        "paper-1", (methods, results)
    )

    assert [item.chunk_index for item in chunks] == list(range(len(chunks)))
    assert all(item.paper_id == "paper-1" for item in chunks)
    assert all(item.metadata.source_section_id for item in chunks)
    assert all(item.page_start <= item.page_end for item in chunks)
    assert not any("ALFF" in item.text and item.section == "Methods" for item in chunks)
    method_pages = {
        (item.page_start, item.page_end) for item in chunks if item.section == "Methods"
    }
    assert method_pages.issubset({(5, 5), (5, 6), (6, 6)})


def test_chunker_uses_token_window_only_for_oversized_sentence() -> None:
    oversized = section(
        "section-long",
        "Discussion",
        (ParagraphSpan(text=" ".join(f"word{i}" for i in range(25)), page_start=9, page_end=9),),
    )
    chunks = ScientificChunker(target_tokens=10, overlap_tokens=2).chunk("paper-1", (oversized,))
    assert len(chunks) == 3
    assert all(item.page_start == item.page_end == 9 for item in chunks)
    assert max(item.token_count for item in chunks) <= 10


def test_chunker_maps_tokenizer_failure_to_stable_signal() -> None:
    class BrokenCounter:
        def count(self, text: str) -> int:
            del text
            raise ValueError("broken")

    item = section(
        "section-1",
        "Methods",
        (ParagraphSpan(text="Some text.", page_start=1, page_end=1),),
    )
    with pytest.raises(RuntimeError, match="tokenizer_failed"):
        ScientificChunker(
            target_tokens=10,
            overlap_tokens=2,
            token_counter=BrokenCounter(),
        ).chunk("paper-1", (item,))


def test_chunker_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="target_tokens"):
        ScientificChunker(target_tokens=0)
    with pytest.raises(ValueError, match="overlap_tokens"):
        ScientificChunker(target_tokens=10, overlap_tokens=10)
