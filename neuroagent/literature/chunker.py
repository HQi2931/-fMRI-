"""Section-aware scientific chunking with page-level provenance."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from neuroagent.literature.models import ChunkMetadata, PaperChunk, PaperSection

_TOKEN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[^\s]", re.UNICODE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])\s+|\n+")


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


class RegexTokenCounter:
    """Deterministic approximation until a model tokenizer is selected."""

    def count(self, text: str) -> int:
        return len(_TOKEN.findall(text))


@dataclass(frozen=True, slots=True)
class _TextSpan:
    text: str
    page_start: int
    page_end: int
    token_count: int


class ScientificChunker:
    def __init__(
        self,
        *,
        target_tokens: int = 600,
        overlap_tokens: int = 100,
        token_counter: TokenCounter | None = None,
    ) -> None:
        if target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if overlap_tokens < 0 or overlap_tokens >= target_tokens:
            raise ValueError("overlap_tokens must be non-negative and smaller than target_tokens")
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.token_counter = token_counter or RegexTokenCounter()

    def chunk(self, paper_id: str, sections: tuple[PaperSection, ...]) -> tuple[PaperChunk, ...]:
        chunks: list[PaperChunk] = []
        for section in sections:
            spans = self._section_spans(section)
            current: list[_TextSpan] = []
            for span in spans:
                if span.token_count > self.target_tokens:
                    if current:
                        self._append_chunk(chunks, paper_id, section, current)
                        current = []
                    fragments = self._split_oversized(span)
                    for fragment in fragments[:-1]:
                        self._append_chunk(chunks, paper_id, section, [fragment])
                    current = list(fragments[-1:])
                    continue
                if current and self._span_tokens(current) + span.token_count > self.target_tokens:
                    self._append_chunk(chunks, paper_id, section, current)
                    current = self._overlap(current)
                    while current and (
                        self._span_tokens(current) + span.token_count > self.target_tokens
                    ):
                        current.pop(0)
                current.append(span)
            if current:
                self._append_chunk(chunks, paper_id, section, current)
        return tuple(chunks)

    def _section_spans(self, section: PaperSection) -> list[_TextSpan]:
        spans: list[_TextSpan] = []
        for paragraph in section.paragraphs:
            sentences = [
                sentence.strip()
                for sentence in _SENTENCE_BOUNDARY.split(paragraph.text)
                if sentence.strip()
            ] or [paragraph.text]
            spans.extend(
                _TextSpan(
                    text=sentence,
                    page_start=paragraph.page_start,
                    page_end=paragraph.page_end,
                    token_count=self._count(sentence),
                )
                for sentence in sentences
            )
        return spans

    def _count(self, text: str) -> int:
        try:
            count = self.token_counter.count(text)
        except Exception as exc:
            raise RuntimeError("tokenizer_failed") from exc
        if count < 1:
            raise RuntimeError("tokenizer_failed")
        return count

    def _split_oversized(self, span: _TextSpan) -> tuple[_TextSpan, ...]:
        tokens = _TOKEN.findall(span.text)
        fragments: list[_TextSpan] = []
        for start in range(0, len(tokens), self.target_tokens):
            text = " ".join(tokens[start : start + self.target_tokens]).strip()
            fragments.append(
                _TextSpan(
                    text=text,
                    page_start=span.page_start,
                    page_end=span.page_end,
                    token_count=self._count(text),
                )
            )
        return tuple(fragments)

    def _overlap(self, spans: list[_TextSpan]) -> list[_TextSpan]:
        selected: list[_TextSpan] = []
        total = 0
        for span in reversed(spans):
            if total + span.token_count > self.overlap_tokens:
                break
            selected.append(span)
            total += span.token_count
        selected.reverse()
        return selected

    @staticmethod
    def _span_tokens(spans: list[_TextSpan]) -> int:
        return sum(span.token_count for span in spans)

    def _append_chunk(
        self,
        chunks: list[PaperChunk],
        paper_id: str,
        section: PaperSection,
        spans: list[_TextSpan],
    ) -> None:
        text = " ".join(span.text for span in spans).strip()
        if not text:
            return
        index = len(chunks)
        chunk_id = str(
            uuid5(
                NAMESPACE_URL,
                f"neuroagent:{paper_id}:chunk:{index}:{section.section_id}:{text}",
            )
        )
        chunks.append(
            PaperChunk(
                chunk_id=chunk_id,
                paper_id=paper_id,
                section=section.section,
                subsection=section.subsection,
                page_start=min(span.page_start for span in spans),
                page_end=max(span.page_end for span in spans),
                text=text,
                token_count=self._count(text),
                chunk_index=index,
                metadata=ChunkMetadata(source_section_id=section.section_id),
            )
        )
