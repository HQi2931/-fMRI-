from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_text_pdf(
    pages: tuple[str, ...], *, title: str | None = None, author: str | None = None
) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for value in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        commands = ["BT /F1 11 Tf 72 740 Td"]
        for index, line in enumerate(value.splitlines()):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if index:
                commands.append("0 -16 Td")
            commands.append(f"({escaped}) Tj")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data(" ".join(commands).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    metadata: dict[str, str] = {"/CreationDate": "D:20240101000000"}
    if title:
        metadata["/Title"] = title
    if author:
        metadata["/Author"] = author
    writer.add_metadata(metadata)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def make_blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
