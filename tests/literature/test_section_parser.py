from __future__ import annotations

from neuroagent.literature.models import PageText
from neuroagent.literature.section_parser import RuleBasedSectionParser


def test_section_parser_recognizes_numbered_aliases_and_method_subsections() -> None:
    pages = (
        PageText(
            page_number=1,
            text=(
                "A Resting State Study\n"
                "Abstract\nSummary text.\n"
                "1 Introduction\nBackground text.\n"
                "2 Materials and Methods\nOverview text.\n"
                "2.1 Subjects\nTwenty participants were included."
            ),
        ),
        PageText(
            page_number=2,
            text=(
                "2.2 Image acquisition\nScanner details.\n"
                "2.3 Data preprocessing\nMotion correction was performed.\n"
                "3 Results\nMain findings."
            ),
        ),
    )

    sections = RuleBasedSectionParser().parse("paper-1", pages)

    labels = [(item.section, item.subsection) for item in sections]
    assert ("Methods", None) in labels
    assert ("Methods", "Participants") in labels
    assert ("Methods", "MRI Acquisition") in labels
    assert ("Methods", "Preprocessing") in labels
    preprocessing = next(item for item in sections if item.subsection == "Preprocessing")
    assert preprocessing.page_start == preprocessing.page_end == 2
    assert preprocessing.metadata["parser"] == "rule_based_section_parser_v1"


def test_section_parser_preserves_unrecognized_text_as_front_matter() -> None:
    sections = RuleBasedSectionParser().parse(
        "paper-2",
        (PageText(page_number=4, text="Unstructured paper text without a heading."),),
    )

    assert len(sections) == 1
    assert sections[0].section == "Front Matter"
    assert sections[0].page_start == 4


def test_section_parser_alias_table_is_replaceable() -> None:
    parser = RuleBasedSectionParser({"Methods": ("experimental setup",)})
    sections = parser.parse(
        "paper-3",
        (PageText(page_number=1, text="Experimental Setup\nDetails are here."),),
    )
    assert sections[0].section == "Methods"
