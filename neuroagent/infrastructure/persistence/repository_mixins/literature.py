"""SQLite persistence for papers, detected sections, and traceable chunks."""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select

from neuroagent.application.errors import NotFoundError
from neuroagent.infrastructure.persistence.models import (
    LiteratureChunkRow,
    PaperRow,
    PaperSectionRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _as_utc,
    _load,
)
from neuroagent.literature.models import (
    ChunkMetadata,
    Paper,
    PaperChunk,
    PaperIngestResult,
    PaperSection,
    ParagraphSpan,
)


class LiteratureMixin(RepositoryBaseMixin):
    def save_literature(self, result: PaperIngestResult) -> None:
        paper = result.paper
        with self._write_session() as session:
            session.add(
                PaperRow(
                    paper_id=paper.paper_id,
                    title=paper.title,
                    authors_json=json.dumps(list(paper.authors), ensure_ascii=False),
                    year=paper.year,
                    journal=paper.journal,
                    doi=paper.doi,
                    abstract=paper.abstract,
                    source_file=paper.source_file,
                    source_sha256=paper.source_sha256,
                    page_count=paper.page_count,
                    metadata_json=json.dumps(paper.metadata, ensure_ascii=False),
                    warnings_json=json.dumps(list(result.warnings), ensure_ascii=False),
                    created_at=paper.created_at,
                )
            )
            # Explicitly establish the parent row before bulk child inserts.
            session.flush()
            session.add_all(
                PaperSectionRow(
                    section_id=item.section_id,
                    paper_id=item.paper_id,
                    section_index=item.section_index,
                    title=item.title,
                    section=item.section,
                    subsection=item.subsection,
                    level=item.level,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    text=item.text,
                    paragraphs_json=json.dumps(
                        [value.model_dump(mode="json") for value in item.paragraphs],
                        ensure_ascii=False,
                    ),
                    metadata_json=json.dumps(item.metadata, ensure_ascii=False),
                )
                for item in result.sections
            )
            session.add_all(
                LiteratureChunkRow(
                    chunk_id=item.chunk_id,
                    paper_id=item.paper_id,
                    section=item.section,
                    subsection=item.subsection,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    text=item.text,
                    token_count=item.token_count,
                    chunk_index=item.chunk_index,
                    metadata_json=json.dumps(
                        item.metadata.model_dump(mode="json"), ensure_ascii=False
                    ),
                )
                for item in result.chunks
            )

    def get_literature(self, paper_id: str) -> PaperIngestResult:
        with self.database.session_factory() as session:
            paper = session.get(PaperRow, paper_id)
            if paper is None:
                raise NotFoundError("paper", paper_id)
            sections = session.scalars(
                select(PaperSectionRow)
                .where(PaperSectionRow.paper_id == paper_id)
                .order_by(PaperSectionRow.section_index)
            ).all()
            chunks = session.scalars(
                select(LiteratureChunkRow)
                .where(LiteratureChunkRow.paper_id == paper_id)
                .order_by(LiteratureChunkRow.chunk_index)
            ).all()
            return self._literature_result(paper, sections, chunks)

    def list_literature(self) -> list[PaperIngestResult]:
        with self.database.session_factory() as session:
            papers = session.scalars(select(PaperRow).order_by(PaperRow.created_at.desc())).all()
            return [
                self._literature_result(
                    paper,
                    session.scalars(
                        select(PaperSectionRow)
                        .where(PaperSectionRow.paper_id == paper.paper_id)
                        .order_by(PaperSectionRow.section_index)
                    ).all(),
                    session.scalars(
                        select(LiteratureChunkRow)
                        .where(LiteratureChunkRow.paper_id == paper.paper_id)
                        .order_by(LiteratureChunkRow.chunk_index)
                    ).all(),
                )
                for paper in papers
            ]

    @staticmethod
    def _literature_result(
        paper: PaperRow,
        sections: Sequence[PaperSectionRow],
        chunks: Sequence[LiteratureChunkRow],
    ) -> PaperIngestResult:
        return PaperIngestResult(
            paper=Paper(
                paper_id=paper.paper_id,
                title=paper.title,
                authors=tuple(_load(paper.authors_json, [])),
                year=paper.year,
                journal=paper.journal,
                doi=paper.doi,
                abstract=paper.abstract,
                source_file=paper.source_file,
                source_sha256=paper.source_sha256,
                page_count=paper.page_count,
                metadata=_load(paper.metadata_json, {}),
                created_at=_as_utc(paper.created_at),
            ),
            sections=tuple(
                PaperSection(
                    section_id=item.section_id,
                    paper_id=item.paper_id,
                    section_index=item.section_index,
                    title=item.title,
                    section=item.section,
                    subsection=item.subsection,
                    level=item.level,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    text=item.text,
                    paragraphs=tuple(
                        ParagraphSpan.model_validate(value)
                        for value in _load(item.paragraphs_json, [])
                    ),
                    metadata=_load(item.metadata_json, {}),
                )
                for item in sections
            ),
            chunks=tuple(
                PaperChunk(
                    chunk_id=item.chunk_id,
                    paper_id=item.paper_id,
                    section=item.section,
                    subsection=item.subsection,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    text=item.text,
                    token_count=item.token_count,
                    chunk_index=item.chunk_index,
                    metadata=ChunkMetadata.model_validate(_load(item.metadata_json, {})),
                )
                for item in chunks
            ),
            warnings=tuple(_load(paper.warnings_json, [])),
        )
