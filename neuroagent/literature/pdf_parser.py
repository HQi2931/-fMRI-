"""Lightweight page-preserving PDF extraction using pypdf."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from neuroagent.literature.models import (
    LiteratureParseError,
    PageText,
    ParsedPdf,
    ParsedPdfMetadata,
)

_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


class PypdfParser:
    """Extract one text record per physical PDF page."""

    def parse(self, content: bytes) -> ParsedPdf:
        if not content.startswith(b"%PDF-"):
            raise LiteratureParseError("unsupported_file_type", "文件内容不是有效的 PDF。")
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise LiteratureParseError("pdf_encrypted", "暂不支持加密 PDF。")
            pages = tuple(
                PageText(page_number=index, text=(page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages, start=1)
            )
            metadata = self._metadata(reader.metadata, pages)
        except LiteratureParseError:
            raise
        except (PdfReadError, EOFError, OSError, TypeError, ValueError) as exc:
            raise LiteratureParseError("pdf_parse_failed", "PDF 已损坏或无法解析。") from exc
        if not pages:
            raise LiteratureParseError("pdf_parse_failed", "PDF 不包含任何页面。")
        if not any(page.text.strip() for page in pages):
            raise LiteratureParseError(
                "pdf_text_layer_missing",
                "PDF 没有可提取的文本层；本阶段尚未启用 OCR。",
            )
        warnings: tuple[str, ...] = ()
        if any(not page.text for page in pages):
            warnings = ("部分页面没有可提取文本，页码仍被保留。",)
        return ParsedPdf(pages=pages, metadata=metadata, warnings=warnings)

    @staticmethod
    def _metadata(raw_metadata: Any, pages: tuple[PageText, ...]) -> ParsedPdfMetadata:
        raw: dict[str, str] = {}
        if raw_metadata:
            for key, value in raw_metadata.items():
                if value is not None:
                    raw[str(key)] = str(value)
        title = _clean(raw.get("/Title"))
        author_text = _clean(raw.get("/Author"))
        authors = tuple(
            part.strip()
            for part in re.split(r"\s*(?:;|\band\b)\s*", author_text or "", flags=re.I)
            if part.strip()
        )
        searchable = "\n".join(page.text for page in pages[:3])
        doi_match = _DOI.search(searchable)
        return ParsedPdfMetadata(
            title=title,
            authors=authors,
            # PDF creation date is not reliable publication-year evidence.
            year=None,
            doi=doi_match.group().rstrip(".,;)") if doi_match else None,
            raw=raw,
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned or None
