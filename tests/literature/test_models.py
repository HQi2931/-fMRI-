from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from neuroagent.literature.models import (
    ChunkMetadata,
    Paper,
    PaperChunk,
    PaperSection,
    ParagraphSpan,
)


def test_paper_fields_may_be_missing_and_source_remains_linked() -> None:
    paper = Paper(
        paper_id="paper-1",
        source_file="literature/paper-1/source.pdf",
        source_sha256="a" * 64,
        page_count=2,
        created_at=datetime.now(UTC),
    )

    assert paper.title is None
    assert paper.authors == ()
    assert paper.source_file.endswith("source.pdf")


def test_chunk_metadata_reserves_rsfmri_fields() -> None:
    chunk = PaperChunk(
        chunk_id="chunk-1",
        paper_id="paper-1",
        section="Methods",
        subsection="Preprocessing",
        page_start=5,
        page_end=6,
        text="Resting-state images were preprocessed.",
        token_count=7,
        chunk_index=0,
        metadata=ChunkMetadata(
            source_section_id="section-1",
            software=("DPABI",),
            metrics=("ALFF",),
            dataset=("ABIDE",),
            harmonization=("ComBat",),
        ),
    )

    assert chunk.metadata.modality == "rs-fMRI"
    assert chunk.metadata.software == ("DPABI",)
    assert chunk.metadata.frequency_band == ()


def test_invalid_page_ranges_fail_validation() -> None:
    with pytest.raises(ValidationError, match="page_end"):
        ParagraphSpan(text="invalid", page_start=3, page_end=2)
    with pytest.raises(ValidationError, match="paragraph page range"):
        PaperSection(
            section_id="section-1",
            paper_id="paper-1",
            title="Methods",
            section="Methods",
            level=1,
            page_start=2,
            page_end=2,
            text="outside",
            paragraphs=(ParagraphSpan(text="outside", page_start=1, page_end=1),),
        )
