"""Extensible rule-based scientific section detection."""

# ruff: noqa: RUF001

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from neuroagent.literature.models import PageText, PaperSection, ParagraphSpan

_NUMBERED = re.compile(
    r"^\s*(?P<number>(?:\d+(?:\.\d+)*|[IVXLC]+))[.)]?\s+(?P<title>.+?)\s*$",
    re.IGNORECASE,
)
_TRAILING_PUNCTUATION = re.compile(r"[\s:：.]+$")

DEFAULT_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "Abstract": ("abstract", "summary"),
    "Introduction": ("introduction", "background"),
    "Methods": (
        "methods",
        "materials and methods",
        "methods and materials",
        "methodology",
        "experimental procedures",
    ),
    "Participants": ("participants", "subjects", "sample", "study population"),
    "MRI Acquisition": (
        "mri acquisition",
        "image acquisition",
        "data acquisition",
        "imaging acquisition",
        "scanning protocol",
    ),
    "Preprocessing": (
        "preprocessing",
        "pre-processing",
        "image preprocessing",
        "data preprocessing",
        "fmri preprocessing",
    ),
    "Feature Extraction": ("feature extraction", "feature calculation"),
    "Statistical Analysis": (
        "statistical analysis",
        "statistical analyses",
        "statistics",
        "data analysis",
    ),
    "Results": ("results", "findings"),
    "Discussion": ("discussion",),
    "Conclusion": ("conclusion", "conclusions"),
    "References": ("references", "bibliography"),
}

_TOP_LEVEL = {
    "Abstract",
    "Introduction",
    "Methods",
    "Results",
    "Discussion",
    "Conclusion",
    "References",
}
_METHOD_SUBSECTIONS = {
    "Participants",
    "MRI Acquisition",
    "Preprocessing",
    "Feature Extraction",
    "Statistical Analysis",
}


@dataclass(slots=True)
class _SectionBuilder:
    title: str
    section: str
    subsection: str | None
    level: int
    paragraphs: list[ParagraphSpan] = field(default_factory=list)


class RuleBasedSectionParser:
    def __init__(self, aliases: dict[str, tuple[str, ...]] | None = None) -> None:
        configured = aliases or DEFAULT_SECTION_ALIASES
        self._aliases = {
            _normalize(alias): canonical
            for canonical, values in configured.items()
            for alias in values
        }

    def parse(self, paper_id: str, pages: tuple[PageText, ...]) -> tuple[PaperSection, ...]:
        builders: list[_SectionBuilder] = []
        current = _SectionBuilder(
            title="Front Matter",
            section="Front Matter",
            subsection=None,
            level=1,
        )
        current_top = "Front Matter"
        for page in pages:
            paragraph_lines: list[str] = []

            for raw_line in page.text.splitlines():
                line = " ".join(raw_line.split()).strip()
                if not line:
                    _flush_paragraph(current, page.page_number, paragraph_lines)
                    continue
                detected = self._detect_heading(line, current_top)
                if detected is None:
                    paragraph_lines.append(line)
                    continue
                _flush_paragraph(current, page.page_number, paragraph_lines)
                if current.paragraphs:
                    builders.append(current)
                canonical, section, subsection, level = detected
                current = _SectionBuilder(
                    title=line,
                    section=section,
                    subsection=subsection,
                    level=level,
                )
                if canonical in _TOP_LEVEL:
                    current_top = canonical
            _flush_paragraph(current, page.page_number, paragraph_lines)
        if current.paragraphs:
            builders.append(current)

        return tuple(
            PaperSection(
                section_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"neuroagent:{paper_id}:section:{index}:{builder.title}:"
                        f"{builder.paragraphs[0].page_start}",
                    )
                ),
                paper_id=paper_id,
                section_index=index,
                title=builder.title,
                section=builder.section,
                subsection=builder.subsection,
                level=builder.level,
                page_start=min(item.page_start for item in builder.paragraphs),
                page_end=max(item.page_end for item in builder.paragraphs),
                text="\n\n".join(item.text for item in builder.paragraphs),
                paragraphs=tuple(builder.paragraphs),
                metadata={"parser": "rule_based_section_parser_v1"},
            )
            for index, builder in enumerate(builders)
        )

    def _detect_heading(
        self, line: str, current_top: str
    ) -> tuple[str, str, str | None, int] | None:
        if len(line) > 120 or line.endswith((".", ";", "?", "!", "。", "；", "？", "！")):
            return None
        numbered = _NUMBERED.match(line)
        number = numbered.group("number") if numbered else None
        candidate = numbered.group("title") if numbered else line
        canonical = self._aliases.get(_normalize(candidate))
        if canonical is None:
            return None
        if canonical in _METHOD_SUBSECTIONS:
            parent = current_top if current_top not in {"Front Matter", "Abstract"} else "Methods"
            level = _number_level(number) if number else 2
            return canonical, parent, canonical, max(2, level)
        level = _number_level(number) if number else 1
        return canonical, canonical, None, level


def _normalize(value: str) -> str:
    return _TRAILING_PUNCTUATION.sub("", value.casefold().strip())


def _number_level(number: str | None) -> int:
    if number is None or not number[:1].isdigit():
        return 1
    return min(number.count(".") + 1, 6)


def _flush_paragraph(
    current: _SectionBuilder, page_number: int, paragraph_lines: list[str]
) -> None:
    if not paragraph_lines:
        return
    text = " ".join(part.strip() for part in paragraph_lines if part.strip()).strip()
    paragraph_lines.clear()
    if text:
        current.paragraphs.append(
            ParagraphSpan(text=text, page_start=page_number, page_end=page_number)
        )
