"""Scientific literature ingestion and traceable chunking."""

from neuroagent.literature.chunker import ScientificChunker
from neuroagent.literature.models import Paper, PaperChunk, PaperIngestResult, PaperSection
from neuroagent.literature.pdf_parser import PypdfParser
from neuroagent.literature.section_parser import RuleBasedSectionParser

__all__ = [
    "Paper",
    "PaperChunk",
    "PaperIngestResult",
    "PaperSection",
    "PypdfParser",
    "RuleBasedSectionParser",
    "ScientificChunker",
]
