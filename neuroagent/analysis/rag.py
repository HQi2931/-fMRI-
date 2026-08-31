"""Small local evidence index with an explicit rs-fMRI scope gate."""

from __future__ import annotations

import re
from pathlib import Path

from neuroagent.analysis.models import EvidenceChunk, RsFmriAnswer

_SCOPE_TERMS = {
    "fmri",
    "rsfmri",
    "rs-fmri",
    "dpabi",
    "dparsfa",
    "spm",
    "alff",
    "falff",
    "reho",
    "bold",
    "scrubbing",
    "voxel",
    "静息态",
    "功能磁共振",
}
# Short ASCII tokens must match on word boundaries so "spm" does not match
# "spm package manager" and "bold" does not match "bold text". Longer tokens
# and CJK terms are specific enough to match as substrings.
_SHORT_ASCII_TERMS = frozenset({"spm", "bold"})
_PROMPT_INJECTION = re.compile(
    r"ignore (previous|all) instructions|system prompt|execute (a )?command|"
    r"run (a )?shell|忽略.*指令|执行.*命令",
    flags=re.IGNORECASE,
)


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", value)}


def is_rsfmri_question(question: str) -> bool:
    lowered = question.lower()
    for term in _SCOPE_TERMS:
        if term in _SHORT_ASCII_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                return True
        elif term in lowered:
            return True
    return False


def search_evidence(
    roots: tuple[Path, ...], query: str, *, limit: int = 5, max_files: int = 5_000
) -> tuple[EvidenceChunk, ...]:
    query_tokens = _tokens(query)
    scored: list[EvidenceChunk] = []
    inspected = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if inspected >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".yaml", ".yml"}:
                continue
            inspected += 1
            text = path.read_text(encoding="utf-8", errors="ignore")
            if _PROMPT_INJECTION.search(text):
                continue
            tokens = _tokens(text)
            score = len(query_tokens & tokens)
            if score == 0:
                continue
            line = next(
                (
                    candidate.strip()
                    for candidate in text.splitlines()
                    if query_tokens & _tokens(candidate)
                ),
                text[:500],
            )
            scored.append(
                EvidenceChunk(
                    source=path.name,
                    title=str(path.relative_to(root)),
                    excerpt=line[:2000],
                    score=float(score),
                )
            )
    return tuple(sorted(scored, key=lambda item: (-item.score, item.source))[:limit])


def answer_rsfmri_question(question: str, evidence: tuple[EvidenceChunk, ...]) -> RsFmriAnswer:
    if not is_rsfmri_question(question):
        return RsFmriAnswer(
            in_scope=False,
            answer="此助手只回答 rs-fMRI, DPABI, SPM, 统计设计和相关参数问题。",
        )
    if not evidence:
        return RsFmriAnswer(
            in_scope=True,
            answer="当前本地知识库没有足够证据, 不能可靠回答; 请补充项目文档或允许检索公开来源。",
        )
    citations = "; ".join(f"[{item.source}]" for item in evidence)
    return RsFmriAnswer(
        in_scope=True,
        answer=f"基于当前检索到的项目证据: {evidence[0].excerpt} 来源: {citations}",
        evidence=evidence,
    )
