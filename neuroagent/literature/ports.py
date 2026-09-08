"""Narrow replaceable ports for literature ingestion."""

from __future__ import annotations

from typing import Protocol

from neuroagent.literature.models import (
    PageText,
    PaperIngestResult,
    PaperSection,
    ParsedPdf,
)


class PdfParser(Protocol):
    def parse(self, content: bytes) -> ParsedPdf: ...


class SectionParser(Protocol):
    def parse(self, paper_id: str, pages: tuple[PageText, ...]) -> tuple[PaperSection, ...]: ...


class LiteratureRepository(Protocol):
    def save_literature(self, result: PaperIngestResult) -> None: ...

    def get_literature(self, paper_id: str) -> PaperIngestResult: ...

    def list_literature(self) -> list[PaperIngestResult]: ...
