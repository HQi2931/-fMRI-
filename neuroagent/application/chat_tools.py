"""Read-only (Tier A) tool catalog for Chat Mode.

Every tool here only observes state.  No Chat tool can start computation, write a
plan or change a run, so the whole Chat catalog stays inside the model-visible
tier by construction.

Tool results are deliberately narrow: the outbound policy rejects an object with
more than 100 list entries or a string longer than 20k characters, so each tool
bounds its own output and reports a total count instead of dumping everything.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.tool_spec import LlmTool, ToolTier
from neuroagent.application.agent_tools import AuditSink, ToolBinding, ToolDispatcher
from neuroagent.chat.evidence import EvidenceCollector
from neuroagent.retrieval.interfaces import RetrievalResult

logger = logging.getLogger(__name__)

MAX_EXCERPT_CHARS = 600
MAX_CHUNKS_PER_SEARCH = 8
MAX_SKILLS_REPORTED = 50

RetrieveCall = Callable[[str, int], Awaitable[RetrievalResult]]
ListSkillsCall = Callable[[], Sequence[Mapping[str, Any]]]

SEARCH_EVIDENCE_TOOL = LlmTool(
    name="search_evidence",
    description=(
        "Search the local rs-fMRI literature index and project documents. "
        "Returns citable passages, each labelled with the citation_id the final "
        "answer must use. Prefer several narrow queries over one broad query."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 2,
                "maxLength": 300,
                "description": "A specific scientific search query.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_CHUNKS_PER_SEARCH,
                "description": "Maximum passages to return.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    tier=ToolTier.READ,
    timeout_seconds=60,
    max_result_bytes=16_384,
)

LIST_SKILLS_TOOL = LlmTool(
    name="list_skills",
    description=(
        "List the reviewed rs-fMRI Skills available in this installation, with "
        "their identifiers and titles. Use this to answer which analyses the "
        "platform supports."
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    tier=ToolTier.READ,
    max_result_bytes=16_384,
)


def build_chat_tool_bindings(
    *,
    retrieve: RetrieveCall,
    collector: EvidenceCollector,
    list_skills: ListSkillsCall | None = None,
) -> list[ToolBinding]:
    """Bind the Chat catalog to injected retrieval and skill-listing callables."""

    async def search_evidence(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        query = str(arguments.get("query") or "").strip()
        raw_limit = arguments.get("limit")
        limit = MAX_CHUNKS_PER_SEARCH if raw_limit is None else int(str(raw_limit))
        limit = max(1, min(limit, MAX_CHUNKS_PER_SEARCH))

        result = await retrieve(query, limit)
        assigned = collector.add(result.chunks[:limit])
        return {
            "in_scope": result.in_scope,
            "returned": len(assigned),
            "passages": [
                {
                    "citation_id": citation_id,
                    "title": chunk.title,
                    "source": chunk.source,
                    "section": chunk.section,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "excerpt": chunk.text[:MAX_EXCERPT_CHARS],
                }
                for citation_id, chunk in assigned
            ],
        }

    def list_available_skills(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        del arguments
        if list_skills is None:
            return {"total": 0, "skills": []}
        skills = list(list_skills())
        return {
            "total": len(skills),
            "skills": [
                {
                    "skill_id": str(item.get("skill_id", "")),
                    "title": str(item.get("title", "")),
                    "status": str(item.get("status", "")),
                }
                for item in skills[:MAX_SKILLS_REPORTED]
            ],
        }

    bindings = [ToolBinding(tool=SEARCH_EVIDENCE_TOOL, handler=search_evidence)]
    bindings.append(ToolBinding(tool=LIST_SKILLS_TOOL, handler=list_available_skills))
    return bindings


def build_chat_dispatcher(
    *,
    retrieve: RetrieveCall,
    collector: EvidenceCollector,
    policy: OutboundContextPolicy,
    list_skills: ListSkillsCall | None = None,
    audit: AuditSink | None = None,
) -> ToolDispatcher:
    """Build the request-scoped Chat dispatcher."""

    return ToolDispatcher(
        build_chat_tool_bindings(
            retrieve=retrieve,
            collector=collector,
            list_skills=list_skills,
        ),
        policy=policy,
        audit=audit,
    )
