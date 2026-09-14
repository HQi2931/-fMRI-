"""Transport-neutral literature, section, and chunk models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LiteratureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RsFmriMetadata(LiteratureModel):
    modality: str = "rs-fMRI"
    topic: str | None = None
    software: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    frequency_band: tuple[str | float, ...] = ()
    atlas: tuple[str, ...] = ()
    dataset: tuple[str, ...] = ()
    harmonization: tuple[str, ...] = ()
    statistics: tuple[str, ...] = ()


class ChunkMetadata(RsFmriMetadata):
    source_section_id: str | None = None
    parser: str = "rule_based_section_parser_v1"
    chunker: str = "scientific_chunker_v1"
    extra: dict[str, Any] = Field(default_factory=dict)


class PageText(LiteratureModel):
    page_number: int = Field(ge=1)
    text: str


class ParsedPdfMetadata(LiteratureModel):
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = Field(default=None, ge=1000, le=3000)
    journal: str | None = None
    doi: str | None = None
    abstract: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)


class ParsedPdf(LiteratureModel):
    pages: tuple[PageText, ...] = Field(min_length=1)
    metadata: ParsedPdfMetadata = Field(default_factory=ParsedPdfMetadata)
    warnings: tuple[str, ...] = ()


class ParagraphSpan(LiteratureModel):
    text: str = Field(min_length=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_pages(self) -> ParagraphSpan:
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        return self


class PaperSection(LiteratureModel):
    section_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    section_index: int = Field(default=0, ge=0)
    title: str = Field(min_length=1)
    section: str = Field(min_length=1)
    subsection: str | None = None
    level: int = Field(ge=1, le=6)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text: str = Field(min_length=1)
    paragraphs: tuple[ParagraphSpan, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_traceability(self) -> PaperSection:
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        if any(
            paragraph.page_start < self.page_start or paragraph.page_end > self.page_end
            for paragraph in self.paragraphs
        ):
            raise ValueError("paragraph page range must remain inside its section")
        return self


IndexStatus = Literal["not_indexed", "indexing", "ready", "failed"]


class Paper(LiteratureModel):
    paper_id: str = Field(min_length=1)
    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = Field(default=None, ge=1000, le=3000)
    journal: str | None = None
    doi: str | None = None
    abstract: str | None = None
    index_status: IndexStatus = "not_indexed"
    index_error: str | None = None
    source_file: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PaperChunk(LiteratureModel):
    chunk_id: str = Field(min_length=1)
    paper_id: str = Field(min_length=1)
    section: str = Field(min_length=1)
    subsection: str | None = None
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text: str = Field(min_length=1)
    token_count: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)

    @model_validator(mode="after")
    def validate_pages(self) -> PaperChunk:
        if self.page_end < self.page_start:
            raise ValueError("page_end must be greater than or equal to page_start")
        return self


class PaperIngestResult(LiteratureModel):
    paper: Paper
    sections: tuple[PaperSection, ...]
    chunks: tuple[PaperChunk, ...]
    warnings: tuple[str, ...] = ()


class LiteratureParseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
