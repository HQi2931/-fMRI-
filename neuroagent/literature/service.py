"""Literature ingestion use case and controlled source-file storage."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import logging
from contextlib import suppress
from pathlib import Path, PurePosixPath
from uuid import uuid4

from neuroagent.application.errors import ApplicationError, InputValidationError, NotFoundError
from neuroagent.literature.chunker import ScientificChunker
from neuroagent.literature.models import Paper, PaperIngestResult
from neuroagent.literature.ports import LiteratureRepository, PdfParser, SectionParser

logger = logging.getLogger(__name__)


class LiteratureService:
    def __init__(
        self,
        *,
        repository: LiteratureRepository,
        work_root: Path,
        pdf_parser: PdfParser,
        section_parser: SectionParser,
        chunker: ScientificChunker,
        max_pdf_bytes: int,
    ) -> None:
        self._repository = repository
        self._work_root = work_root.resolve()
        self._pdf_parser = pdf_parser
        self._section_parser = section_parser
        self._chunker = chunker
        self.max_pdf_bytes = max_pdf_bytes

    def ingest(self, *, filename: str, content: bytes) -> PaperIngestResult:
        safe_name = Path(filename).name
        if Path(safe_name).suffix.casefold() != ".pdf":
            raise ApplicationError(
                "unsupported_file_type",
                "只支持 PDF 文献文件。",
                status_code=415,
            )
        if not content:
            raise InputValidationError("pdf_empty", "上传的 PDF 文件为空。")
        if len(content) > self.max_pdf_bytes:
            raise ApplicationError(
                "pdf_too_large",
                "PDF 超过当前配置的大小限制。",
                status_code=413,
                details={"max_bytes": self.max_pdf_bytes},
            )
        if not content.startswith(b"%PDF-"):
            raise ApplicationError(
                "unsupported_file_type",
                "文件扩展名为 PDF，但内容不是 PDF。",
                status_code=415,
            )

        try:
            parsed = self._pdf_parser.parse(content)
            paper_id = str(uuid4())
            sections = self._section_parser.parse(paper_id, parsed.pages)
            if not sections:
                raise InputValidationError(
                    "section_parse_failed",
                    "PDF 有文本，但未能形成可用的论文段落。",
                )
            chunks = self._chunker.chunk(paper_id, sections)
            if not chunks:
                raise InputValidationError(
                    "chunking_failed",
                    "论文文本未能生成可用 chunk。",
                )
        except InputValidationError:
            raise
        except RuntimeError as exc:
            code = getattr(exc, "code", str(exc))
            if code == "tokenizer_failed":
                raise InputValidationError(
                    "tokenizer_failed", "论文分块时 token 计数失败。"
                ) from exc
            if code in {
                "pdf_parse_failed",
                "pdf_encrypted",
                "pdf_text_layer_missing",
                "unsupported_file_type",
            }:
                status_code = 415 if code == "unsupported_file_type" else 422
                raise ApplicationError(
                    code,
                    getattr(exc, "message", "PDF 解析失败。"),
                    status_code=status_code,
                ) from exc
            raise

        source_sha256 = hashlib.sha256(content).hexdigest()
        relative_source = PurePosixPath("literature", paper_id, "source.pdf").as_posix()
        abstract = next(
            (section.text for section in sections if section.section == "Abstract"),
            parsed.metadata.abstract,
        )
        title = parsed.metadata.title or _probable_title(parsed.pages[0].text)
        warnings = list(parsed.warnings)
        if not any(section.section != "Front Matter" for section in sections):
            warnings.append("未识别到标准章节标题，文本已保留为 Front Matter。")
        paper = Paper(
            paper_id=paper_id,
            title=title,
            authors=parsed.metadata.authors,
            year=parsed.metadata.year,
            journal=parsed.metadata.journal,
            doi=parsed.metadata.doi,
            abstract=abstract,
            source_file=relative_source,
            source_sha256=source_sha256,
            page_count=len(parsed.pages),
            metadata={
                "parser": "pypdf",
                "original_filename": safe_name,
                "raw_pdf_metadata": parsed.metadata.raw,
            },
        )
        result = PaperIngestResult(
            paper=paper,
            sections=sections,
            chunks=chunks,
            warnings=tuple(warnings),
        )
        target = self._work_root.joinpath(*PurePosixPath(relative_source).parts)
        temporary = target.with_suffix(".uploading")
        target.parent.mkdir(parents=True, exist_ok=False)
        try:
            temporary.write_bytes(content)
            temporary.replace(target)
            self._repository.save_literature(result)
        except BaseException:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            with suppress(OSError):
                target.parent.rmdir()
            raise
        logger.info(
            "Literature ingested paper_id=%s pages=%d sections=%d chunks=%d",
            paper_id,
            paper.page_count,
            len(sections),
            len(chunks),
        )
        return result

    def get(self, paper_id: str) -> PaperIngestResult:
        try:
            return self._repository.get_literature(paper_id)
        except NotFoundError:
            raise

    def list(self) -> list[PaperIngestResult]:
        return self._repository.list_literature()


def _probable_title(first_page: str) -> str | None:
    for line in first_page.splitlines():
        candidate = " ".join(line.split()).strip()
        if 8 <= len(candidate) <= 300 and not candidate.casefold().startswith(
            ("abstract", "doi", "http")
        ):
            return candidate
    return None
