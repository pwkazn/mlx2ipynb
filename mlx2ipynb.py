#!/usr/bin/env python3
"""Convert MATLAB Live Script (.mlx) to Jupyter Notebook (.ipynb), MATLAB script (.m), and Markdown (.md)."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Namespaces used in MLX files
# ---------------------------------------------------------------------------
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Run:
    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    monospace: bool = False


@dataclass
class InlineImage:
    rel_id: str = ""


@dataclass
class Paragraph:
    style: str = "text"
    runs: list[Run | InlineImage] = field(default_factory=list)
    num_id: str | None = None
    align: str | None = None


@dataclass
class Cell:
    kind: str = "text"  # "text" | "code"
    paragraphs: list[Paragraph] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MlixFile:
    document_xml: str = ""
    output_xml: str = ""
    rels: dict[str, str] = field(default_factory=dict)
    media: dict[str, bytes] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parser utility
# ---------------------------------------------------------------------------
def _ns(tag: str) -> str:
    """Expand a qualified tag name like 'w:p' to the full namespace form."""
    prefix, local = tag.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


# ---------------------------------------------------------------------------
# MLX reader
# ---------------------------------------------------------------------------
def read_mlx(path: Path) -> MlixFile:
    """Read an .mlx ZIP archive and return structured contents."""
    mlx = MlixFile()

    with zipfile.ZipFile(path, "r") as z:
        mlx.document_xml = z.read("matlab/document.xml").decode("utf-8")
        mlx.output_xml = z.read("matlab/output.xml").decode("utf-8")

        # Read relationships for document (maps rId → target path, optional)
        try:
            rels_xml = z.read("matlab/_rels/document.xml.rels").decode("utf-8")
            rels_root = ET.fromstring(rels_xml)
            for child in rels_root:
                rid = child.attrib.get("Id", "")
                target = child.attrib.get("Target", "")
                mlx.rels[rid] = target
        except KeyError:
            pass

        # Read embedded media (images)
        for name in z.namelist():
            if name.startswith("media/") and not name.endswith("/"):
                mlx.media[name] = z.read(name)

    return mlx


# ---------------------------------------------------------------------------
# Document parser
# ---------------------------------------------------------------------------
def _get_style(p_elem: ET.Element) -> str:
    """Get the paragraph style from a w:p element."""
    ppr = p_elem.find(_ns("w:pPr"))
    if ppr is not None:
        pstyle = ppr.find(_ns("w:pStyle"))
        if pstyle is not None:
            return pstyle.attrib.get(f"{{{NS['w']}}}val", "text")

    # Check alternate content (mc:AlternateContent → mc:Choice / mc:Fallback)
    mc_alt = p_elem.find(_ns("mc:AlternateContent"))
    if mc_alt is not None:
        choice = mc_alt.find(_ns("mc:Choice"))
        if choice is not None:
            ppr2 = choice.find(_ns("w:pPr"))
            if ppr2 is not None:
                pstyle2 = ppr2.find(_ns("w:pStyle"))
                if pstyle2 is not None:
                    return pstyle2.attrib.get(f"{{{NS['w']}}}val", "text")
        fallback = mc_alt.find(_ns("mc:Fallback"))
        if fallback is not None:
            ppr2 = fallback.find(_ns("w:pPr"))
            if ppr2 is not None:
                pstyle2 = ppr2.find(_ns("w:pStyle"))
                if pstyle2 is not None:
                    return pstyle2.attrib.get(f"{{{NS['w']}}}val", "text")

    return "text"


def _parse_runs(p_elem: ET.Element) -> list[Run | InlineImage]:
    """Extract runs (w:r, w:customXml) from a w:p element."""
    items: list[Run | InlineImage] = []

    for child in p_elem:
        tag = child.tag

        # Normal text run
        if tag == _ns("w:r"):
            rpr = child.find(_ns("w:rPr"))
            run = Run()
            if rpr is not None:
                run.bold = rpr.find(_ns("w:b")) is not None
                run.italic = rpr.find(_ns("w:i")) is not None
                run.underline = rpr.find(_ns("w:u")) is not None
                rfonts = rpr.find(_ns("w:rFonts"))
                if rfonts is not None:
                    cs = rfonts.attrib.get(f"{{{NS['w']}}}cs", "")
                    run.monospace = bool(cs and cs.lower() != "ansi")
            # Get text
            texts: list[str] = []
            for t in child.iter(_ns("w:t")):
                if t.text:
                    texts.append(t.text)
            run.text = "".join(texts)
            items.append(run)

        # Alternate content (R2018a+ code blocks)
        elif tag == _ns("mc:AlternateContent"):
            for ac_child in child:
                sdt = ac_child.find(_ns("w:sdt"))
                if sdt is not None:
                    sdt_content = sdt.find(_ns("w:sdtContent"))
                    if sdt_content is not None:
                        for p2 in sdt_content.findall(_ns("w:p")):
                            items.extend(_parse_runs(p2))
                else:
                    # Direct w:p in alternate content
                    for p2 in ac_child.findall(_ns("w:p")):
                        items.extend(_parse_runs(p2))

        # Inline image
        elif (
            tag == _ns("w:customXml")
            and child.attrib.get(f"{{{NS['w']}}}element", "") == "image"
        ):
            img = InlineImage()
            cxml_pr = child.find(_ns("w:customXmlPr"))
            if cxml_pr is not None:
                for attr in cxml_pr.findall(_ns("w:attr")):
                    if attr.attrib.get(f"{{{NS['w']}}}name", "") == "relationshipId":
                        img.rel_id = attr.attrib.get(f"{{{NS['w']}}}val", "")
            items.append(img)

        # DrawingML image (inline)
        elif tag == _ns("w:r"):
            drawing = child.find(_ns("w:drawing"))
            if drawing is not None:
                blip = drawing.find(".//" + _ns("a:blip")) or drawing.find(
                    ".//" + f"{{{NS['r']}}}blip"
                )
                if blip is not None:
                    img = InlineImage()
                    for k, v in blip.attrib.items():
                        if "embed" in k:
                            img.rel_id = v
                    items.append(img)

    return items


def _has_sect_break(p_elem: ET.Element) -> bool:
    """Check if a paragraph contains a section break (w:sectPr)."""
    # Direct sectPr
    ppr = p_elem.find(_ns("w:pPr"))
    if ppr is not None and ppr.find(_ns("w:sectPr")) is not None:
        return True
    # Alternate content with sectPr
    for mc_alt in p_elem.findall(_ns("mc:AlternateContent")):
        for sub in mc_alt:
            ppr2 = sub.find(_ns("w:pPr"))
            if ppr2 is not None and ppr2.find(_ns("w:sectPr")) is not None:
                return True
    return False


def is_code_style(style: str) -> bool:
    return style in ("code", "CodeExampleLine")


HEADING_STYLES = {
    "heading",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "heading7",
    "heading8",
    "heading9",
}


def is_text_style(style: str) -> bool:
    return (
        style
        in (
            "text",
            "ListParagraph",
            "title",
            "Subtitle",
        )
        or style in HEADING_STYLES
    )


def parse_cells(mlx: MlixFile) -> list[Cell]:
    """Parse document.xml into a list of Cells (text or code)."""
    root = ET.fromstring(mlx.document_xml)
    body = root.find(_ns("w:body"))
    if body is None:
        return []

    paragraphs: list[Paragraph] = []
    for p_elem in body.findall(_ns("w:p")):
        style = _get_style(p_elem)
        runs = _parse_runs(p_elem)

        # Skip completely empty paragraphs
        if not runs and style == "text":
            continue

        num_id: str | None = None
        align: str | None = None
        ppr = p_elem.find(_ns("w:pPr"))
        if ppr is not None:
            numpr = ppr.find(_ns("w:numPr"))
            if numpr is not None:
                ni = numpr.find(_ns("w:numId"))
                if ni is not None:
                    num_id = ni.get(f"{{{NS['w']}}}val")
            jc = ppr.find(_ns("w:jc"))
            if jc is not None:
                align = jc.get(f"{{{NS['w']}}}val")

        paragraph = Paragraph(style=style, runs=runs, num_id=num_id, align=align)
        paragraphs.append(paragraph)

    # Group paragraphs into alternating text/code cells
    cells: list[Cell] = []
    buffer: list[Paragraph] = []
    buffer_type: str | None = None

    def flush():
        if buffer:
            cells.append(Cell(kind=buffer_type or "text", paragraphs=list(buffer)))
            buffer.clear()

    for p in paragraphs:
        code = is_code_style(p.style)
        txt = is_text_style(p.style)

        if code:
            kind = "code"
        elif txt:
            kind = "text"
        else:
            kind = buffer_type or "text"

        if buffer_type is None:
            buffer_type = kind
            buffer.append(p)
        elif kind == buffer_type:
            buffer.append(p)
        else:
            flush()
            buffer_type = kind
            buffer.append(p)

    flush()

    # Filter out text cells with no substantive content
    def is_substantive(cell: Cell) -> bool:
        if cell.kind == "code":
            return True
        for p in cell.paragraphs:
            for item in p.runs:
                if isinstance(item, Run) and item.text.strip():
                    return True
                if isinstance(item, InlineImage):
                    return True
        return False

    cells = [c for c in cells if is_substantive(c)]
    return cells


# ---------------------------------------------------------------------------
# Output parser
# ---------------------------------------------------------------------------
def parse_outputs(mlx: MlixFile) -> list[dict[str, Any]]:
    """Extract output elements from output.xml (no namespace in this file)."""
    outputs: list[dict[str, Any]] = []
    root = ET.fromstring(mlx.output_xml)

    output_array = root.find("outputArray")
    if output_array is None:
        return outputs

    for elem in output_array.findall("element"):
        out: dict[str, Any] = {"type": "unknown", "data": None}

        type_elem = elem.find("type")
        if type_elem is not None and type_elem.text:
            out["type"] = type_elem.text

        data_elem = elem.find("outputData")
        if data_elem is not None:
            if out["type"] == "figure":
                fig_uri = data_elem.find("figureUri")
                if fig_uri is not None and fig_uri.text:
                    uri = fig_uri.text.strip()
                    if uri.startswith("data:image/png;base64,"):
                        b64 = uri[len("data:image/png;base64,") :]
                        out["data"] = {"image/png": b64}
            else:
                text_content = "".join(data_elem.itertext()).strip()
                if text_content:
                    out["data"] = {"text/plain": text_content}

        outputs.append(out)

    return outputs


def map_outputs_to_cells(cells: list[Cell], outputs: list[dict[str, Any]]) -> None:
    """Assign outputs to code cells in order."""
    out_idx = 0
    for cell in cells:
        if cell.kind == "code" and out_idx < len(outputs):
            cell.outputs.append(outputs[out_idx])
            out_idx += 1


# ---------------------------------------------------------------------------
# Text / Code extraction
# ---------------------------------------------------------------------------
def _escape_md(text: str) -> str:
    """Escape markdown metacharacters so they render as literals."""
    text = text.replace("\\", "\\\\")
    text = text.replace("*", "\\*")
    text = text.replace("_", "\\_")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_to_markdown(run: Run) -> str:
    """Convert a single Run to markdown text."""
    text = run.text
    if not text:
        return ""

    needs_html = run.underline or (run.monospace and (run.bold or run.italic))

    if needs_html:
        text = _escape_html(text)
        if run.monospace:
            text = f"<code>{text}</code>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.underline:
            text = f"<u>{text}</u>"
        return text

    text = _escape_md(text)
    if run.monospace:
        text = f"`{text}`"
    elif run.bold and run.italic:
        text = f"***{text}***"
    elif run.bold:
        text = f"**{text}**"
    elif run.italic:
        text = f"*{text}*"
    return text


def _img_html(src: str) -> str:
    return f'<img src="{src}" alt="image">'


def paragraph_to_markdown(
    p: Paragraph,
    rels: dict[str, str],
    mlx: MlixFile | None = None,
    use_html: bool = False,
) -> str:
    """Convert a Paragraph to a markdown string.
    Set use_html=True when this text will be embedded inside an HTML block
    (e.g. alignment divs), so images use <img> instead of markdown ![]().
    """
    parts: list[str] = []
    for item in p.runs:
        if isinstance(item, Run):
            parts.append(run_to_markdown(item))
        elif isinstance(item, InlineImage):
            target = rels.get(item.rel_id, "")
            if target and mlx is not None:
                media_name = target.replace("\\", "/").lstrip("../")
                if media_name in mlx.media:
                    import base64

                    b64 = base64.b64encode(mlx.media[media_name]).decode("ascii")
                    src = f"data:image/png;base64,{b64}"
                else:
                    src = target
                parts.append(_img_html(src) if use_html else f"![image]({src})")
    return "".join(parts)


def paragraph_to_plain(p: Paragraph, rels: dict[str, str]) -> str:
    """Convert a Paragraph to plain text (no markdown formatting)."""
    parts: list[str] = []
    for item in p.runs:
        if isinstance(item, Run):
            parts.append(item.text)
        elif isinstance(item, InlineImage):
            parts.append("[image]")
    return "".join(parts)


def _heading_level(style: str) -> int:
    """Convert a heading style name to a markdown heading level (1-6).
    title→H1, heading→H2, heading2→H3, heading3→H4, etc."""
    if style == "title":
        return 1
    if style == "heading":
        return 2
    if style.startswith("heading"):
        try:
            n = int(style[7:])
            return min(n + 1, 6)
        except (ValueError, IndexError):
            return 2
    return 0


def text_cell_to_markdown(
    cell: Cell, rels: dict[str, str], mlx: MlixFile | None = None
) -> str:
    """Convert a text cell (multiple Paragraphs) to a markdown block."""
    md_paragraphs: list[str] = []
    for p in cell.paragraphs:
        in_html = bool(p.align and p.align != "left")
        text = paragraph_to_markdown(p, rels, mlx, use_html=in_html)
        style = p.style
        stripped = text.strip()
        if not stripped:
            continue

        level = _heading_level(style)
        if level:
            if p.align and p.align != "left":
                md_paragraphs.append(
                    f'<h{level} style="text-align:{p.align}">{stripped}</h{level}>'
                )
            else:
                md_paragraphs.append(f"{'#' * level} {stripped}")
        elif style == "ListParagraph":
            prefix = "1. " if p.num_id and p.num_id != "1" else "- "
            md_paragraphs.append(f"{prefix}{stripped}")
        elif p.align and p.align != "left":
            html_align = f' style="text-align:{p.align}"'
            md_paragraphs.append(f"<div{html_align}>{stripped}</div>")
        else:
            md_paragraphs.append(stripped)

    return "\n\n".join(md_paragraphs)


def code_cell_to_text(cell: Cell) -> str:
    """Extract MATLAB code from a code cell."""
    code_lines: list[str] = []
    for p in cell.paragraphs:
        for item in p.runs:
            if isinstance(item, Run):
                text = item.text
                # Handle CDATA-wrapped code (may contain newlines)
                if text:
                    code_lines.append(text)
    return "\n".join(code_lines)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def write_ipynb(cells: list[Cell], mlx: MlixFile, path: Path) -> None:
    """Write a Jupyter Notebook (.ipynb)."""
    nb_cells: list[dict[str, Any]] = []

    for cell in cells:
        if cell.kind == "text":
            source = text_cell_to_markdown(cell, mlx.rels, mlx)
            if not source.strip():
                continue
            nb_cells.append(
                {
                    "cell_type": "markdown",
                    "metadata": {},
                    "source": split_lines(source),
                }
            )
        else:
            source = code_cell_to_text(cell)
            if not source.strip():
                continue

            # Build outputs
            jupyter_outputs: list[dict[str, Any]] = []
            for out in cell.outputs:
                if out["type"] == "figure" and out["data"]:
                    jupyter_outputs.append(
                        {
                            "output_type": "display_data",
                            "data": {
                                "image/png": out["data"]["image/png"],
                                "text/plain": ["<Figure>"],
                            },
                            "metadata": {},
                        }
                    )
                elif out["data"]:
                    jupyter_outputs.append(
                        {
                            "output_type": "execute_result"
                            if out["type"] == "inlineOutput"
                            else "stream",
                            "data": out["data"],
                            "metadata": {},
                        }
                    )

            nb_cells.append(
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "metadata": {},
                    "outputs": jupyter_outputs,
                    "source": split_lines(source),
                }
            )

    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "MATLAB",
                "language": "matlab",
                "name": "matlab",
            },
            "language_info": {
                "name": "matlab",
                "version": "",
            },
        },
        "cells": nb_cells,
    }

    path.write_text(
        json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def split_lines(text: str) -> list[str]:
    """Split text into lines (adding newline terminators, Jupyter convention)."""
    lines = text.split("\n")
    return [line + "\n" for line in lines]


def write_m(cells: list[Cell], path: Path) -> None:
    """Write a plain MATLAB script (.m) — code cells only."""
    code_blocks: list[str] = []
    for cell in cells:
        if cell.kind == "code":
            code = code_cell_to_text(cell).strip()
            if code:
                code_blocks.append(code)

    path.write_text("\n\n".join(code_blocks) + "\n", encoding="utf-8")


def write_md(cells: list[Cell], mlx: MlixFile, path: Path) -> None:
    """Write Markdown (.md) with rich text and fenced code blocks."""
    md_parts: list[str] = []

    for cell in cells:
        if cell.kind == "text":
            md = text_cell_to_markdown(cell, mlx.rels, mlx)
            if md.strip():
                md_parts.append(md)
        else:
            code = code_cell_to_text(cell).strip()
            if code:
                md_parts.append(f"```matlab\n{code}\n```")

    path.write_text("\n\n".join(md_parts) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert MATLAB Live Script (.mlx) to other formats."
    )
    parser.add_argument("input", type=Path, help="Input .mlx file")
    parser.add_argument(
        "--format",
        "-f",
        nargs="+",
        choices=["ipynb", "m", "md", "all"],
        default=["all"],
        help="Output format(s) to generate (default: all)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output file or directory (default: same as input with new extension)",
    )
    args = parser.parse_args()

    input_path: Path = args.input
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    formats = args.format
    if "all" in formats:
        formats = ["ipynb", "m", "md"]

    print(f"Reading {input_path}...")
    mlx = read_mlx(input_path)

    print("Parsing document...")
    cells = parse_cells(mlx)

    print(
        f"Found {len(cells)} cells ({sum(1 for c in cells if c.kind == 'text')} text, {sum(1 for c in cells if c.kind == 'code')} code)"
    )

    print("Parsing outputs...")
    outputs = parse_outputs(mlx)
    map_outputs_to_cells(cells, outputs)
    print(f"Found {len(outputs)} outputs")

    stem = input_path.stem

    if "ipynb" in formats:
        out = (
            args.output.with_suffix(".ipynb")
            if args.output
            else input_path.with_suffix(".ipynb")
        )
        write_ipynb(cells, mlx, out)
        print(f"  Wrote {out}")

    if "m" in formats:
        out = (
            args.output.with_suffix(".m")
            if args.output
            else input_path.with_suffix(".m")
        )
        write_m(cells, out)
        print(f"  Wrote {out}")

    if "md" in formats:
        out = (
            args.output.with_suffix(".md")
            if args.output
            else input_path.with_suffix(".md")
        )
        write_md(cells, mlx, out)
        print(f"  Wrote {out}")

    print("Done.")


if __name__ == "__main__":
    main()
