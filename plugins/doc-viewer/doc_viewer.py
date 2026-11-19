#!/usr/bin/env python3
"""
Rippflow Filetype Handler Plugin: PDF / DOCX / PPTX Document Viewer.

Implements the `rippflow-agent/v1` `open_file` and `file_event` actions using
only the Python 3 standard library. Emits a Declarative UI specification
leveraging Tier 1 (`Toolbar`, `Pager`, `Markdown`, `Image`, `Table`,
`MetricCard`) and Tier 2 (`Canvas`) native GPUI primitives.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024.0:.1f} KB"
    return f"{num_bytes / (1024.0 * 1024.0):.2f} MB"


def make_rgba_banner(width: int, height: int, accent_rgb: tuple, bg_rgb: tuple) -> str:
    """Generates a clean RGBA8 framebuffer (base64) for Canvas fallback/preview."""
    r0, g0, b0 = bg_rgb
    ra, ga, ba = accent_rgb
    buf = bytearray(width * height * 4)
    header_h = max(12, height // 6)
    for y in range(height):
        for x in range(width):
            idx = (y * width + x) * 4
            is_border = x < 2 or x >= width - 2 or y < 2 or y >= height - 2
            if is_border or y < header_h:
                buf[idx] = ra
                buf[idx + 1] = ga
                buf[idx + 2] = ba
                buf[idx + 3] = 255
            elif (
                y > header_h + 14
                and y < height - 14
                and (y - header_h) % 16 < 5
                and 18 <= x <= width - (28 if ((y - header_h) // 16) % 3 == 2 else 18)
            ):
                buf[idx] = min(255, r0 + 36)
                buf[idx + 1] = min(255, g0 + 40)
                buf[idx + 2] = min(255, b0 + 48)
                buf[idx + 3] = 255
            else:
                buf[idx] = r0
                buf[idx + 1] = g0
                buf[idx + 2] = b0
                buf[idx + 3] = 255
    return base64.b64encode(bytes(buf)).decode("ascii")


def parse_ooxml_core_props(zf: zipfile.ZipFile) -> dict:
    props = {}
    if "docProps/core.xml" not in zf.namelist():
        return props
    try:
        root = ET.fromstring(zf.read("docProps/core.xml"))
        for elem in root:
            tag = elem.tag.split("}", 1)[-1]
            if elem.text and elem.text.strip():
                props[tag] = elem.text.strip()
    except Exception:
        pass
    return props


def convert_office_to_pdf_cached(file_path: Path) -> Path | None:
    """Converts a .docx or .pptx file to a cached PDF via LibreOffice (soffice) if available."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice or not file_path.is_file():
        return None
    try:
        # Only invoke LibreOffice on full OOXML packages containing [Content_Types].xml
        with zipfile.ZipFile(file_path, "r") as zf:
            if "[Content_Types].xml" not in zf.namelist():
                return None
    except Exception:
        return None
    try:
        st = file_path.stat()
        key = hashlib.sha256(
            f"{file_path.resolve()}:{st.st_mtime_ns}:{st.st_size}".encode("utf-8")
        ).hexdigest()[:24]
        cache_dir = Path(tempfile.gettempdir()) / "rippflow-doc-viewer-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached_pdf = cache_dir / f"{key}.pdf"
        if cached_pdf.is_file() and cached_pdf.stat().st_size > 0:
            return cached_pdf

        with tempfile.TemporaryDirectory(prefix="rippflow-soffice-") as tmp:
            tmp_dir = Path(tmp)
            profile_dir = tmp_dir / "lo-profile"
            res = subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file://{profile_dir}",
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmp_dir),
                    str(file_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            if res.returncode == 0:
                pdfs = sorted(tmp_dir.glob("*.pdf"))
                if pdfs and pdfs[0].stat().st_size > 0:
                    shutil.copyfile(pdfs[0], cached_pdf)
                    return cached_pdf
    except Exception:
        pass
    return None


def render_styled_slide_png_macos(
    title: str,
    bullets: list[str],
    slide_num: int,
    total_slides: int,
    deck_name: str,
) -> dict | None:
    """Renders a crisp 16:9 visual presentation slide via built-in macOS AppKit/CoreGraphics."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    jxa = r"""
ObjC.import("AppKit");
ObjC.import("Foundation");
function run(argv) {
  const payload = JSON.parse(argv[0]);
  const outPath = argv[1];
  const W = 1280, H = 720;
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, W, H, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0
  );
  const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);

  // Slide canvas background (clean paper-white with subtle cool tint)
  $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.985, 0.988, 0.995, 1.0).setFill;
  $.NSRectFill($.NSMakeRect(0, 0, W, H));

  // Top accent bar + header band
  $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.06, 0.11, 0.21, 1.0).setFill;
  $.NSRectFill($.NSMakeRect(0, H - 150, W, 150));
  $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.22, 0.74, 0.97, 1.0).setFill;
  $.NSRectFill($.NSMakeRect(0, H - 156, W, 6));

  // Subtle footer line
  $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.86, 0.89, 0.94, 1.0).setFill;
  $.NSRectFill($.NSMakeRect(64, 58, W - 128, 1.5));

  // Slide title
  const titleAttrs = $.NSMutableDictionary.dictionary;
  titleAttrs.setObjectForKey($.NSFont.boldSystemFontOfSize(42), $.NSFontAttributeName);
  titleAttrs.setObjectForKey($.NSColor.whiteColor, $.NSForegroundColorAttributeName);
  $(payload.title || "Untitled Slide").drawInRectWithAttributes($.NSMakeRect(64, H - 128, W - 128, 96), titleAttrs);

  // Bullets
  const bulletAttrs = $.NSMutableDictionary.dictionary;
  bulletAttrs.setObjectForKey($.NSFont.systemFontOfSize(26), $.NSFontAttributeName);
  bulletAttrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.11, 0.15, 0.23, 1.0), $.NSForegroundColorAttributeName);
  const dotColor = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.02, 0.52, 0.78, 1.0);
  let y = H - 230;
  const bullets = payload.bullets || [];
  for (let i = 0; i < Math.min(bullets.length, 8); i++) {
    if (y < 90) break;
    dotColor.setFill;
    $.NSBezierPath.bezierPathWithOvalInRect($.NSMakeRect(70, y + 10, 10, 10)).fill;
    $(String(bullets[i])).drawInRectWithAttributes($.NSMakeRect(96, y - 34, W - 160, 64), bulletAttrs);
    y -= 68;
  }

  // Footer metadata & slide badge
  const footerAttrs = $.NSMutableDictionary.dictionary;
  footerAttrs.setObjectForKey($.NSFont.systemFontOfSize(16), $.NSFontAttributeName);
  footerAttrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.42, 0.48, 0.57, 1.0), $.NSForegroundColorAttributeName);
  $(payload.deck_name || "").drawInRectWithAttributes($.NSMakeRect(64, 22, 700, 26), footerAttrs);
  $("Slide " + payload.slide_num + " / " + payload.total_slides).drawInRectWithAttributes($.NSMakeRect(W - 210, 22, 146, 26), footerAttrs);

  $.NSGraphicsContext.restoreGraphicsState;
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
  png.writeToFileAtomically(outPath, true);
  return JSON.stringify({ width: 960, height: 540 });
}
"""
    with tempfile.TemporaryDirectory(prefix="rippflow-slide-img-") as tmp:
        out_png = Path(tmp) / f"slide-{slide_num}.png"
        try:
            data = json.dumps({
                "title": title,
                "bullets": bullets,
                "slide_num": slide_num,
                "total_slides": total_slides,
                "deck_name": deck_name,
            })
            res = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", jxa, data, str(out_png)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0:
                return {
                    "png_base64": base64.b64encode(out_png.read_bytes()).decode("ascii"),
                    "width": 960,
                    "height": 540,
                    "count": total_slides,
                }
        except Exception:
            pass
    return None


def render_styled_docx_page_png_macos(
    title: str,
    paragraphs: list[str],
    tables: list,
    page: int,
) -> dict | None:
    """Renders a crisp Word-style letter paper page via built-in macOS AppKit/CoreGraphics."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    jxa = r"""
ObjC.import("AppKit");
ObjC.import("Foundation");
function run(argv) {
  const payload = JSON.parse(argv[0]);
  const outPath = argv[1];
  const W = 1224, H = 1584; // US Letter @ 144 DPI (2x)
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, W, H, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0
  );
  const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);

  // Paper background + subtle page border
  $.NSColor.whiteColor.setFill;
  $.NSRectFill($.NSMakeRect(0, 0, W, H));
  $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.86, 0.88, 0.92, 1.0).setStroke;
  $.NSBezierPath.strokeRect($.NSMakeRect(1, 1, W - 2, H - 2));

  // Header & Footer
  const metaAttrs = $.NSMutableDictionary.dictionary;
  metaAttrs.setObjectForKey($.NSFont.systemFontOfSize(18), $.NSFontAttributeName);
  metaAttrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.45, 0.50, 0.58, 1.0), $.NSForegroundColorAttributeName);
  $(payload.title || "").drawInRectWithAttributes($.NSMakeRect(108, H - 72, W - 216, 28), metaAttrs);
  $("Page " + payload.page).drawInRectWithAttributes($.NSMakeRect(W - 220, 44, 112, 28), metaAttrs);

  const h1Attrs = $.NSMutableDictionary.dictionary;
  h1Attrs.setObjectForKey($.NSFont.boldSystemFontOfSize(38), $.NSFontAttributeName);
  h1Attrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.08, 0.13, 0.24, 1.0), $.NSForegroundColorAttributeName);

  const h2Attrs = $.NSMutableDictionary.dictionary;
  h2Attrs.setObjectForKey($.NSFont.boldSystemFontOfSize(28), $.NSFontAttributeName);
  h2Attrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.10, 0.34, 0.58, 1.0), $.NSForegroundColorAttributeName);

  const bodyAttrs = $.NSMutableDictionary.dictionary;
  bodyAttrs.setObjectForKey($.NSFont.systemFontOfSize(22), $.NSFontAttributeName);
  bodyAttrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.14, 0.16, 0.20, 1.0), $.NSForegroundColorAttributeName);

  let y = H - 130;
  const paras = payload.paragraphs || [];
  for (let i = 0; i < paras.length; i++) {
    if (y < 140) break;
    const raw = String(paras[i]);
    if (raw.startsWith("# ")) {
      $(raw.slice(2)).drawInRectWithAttributes($.NSMakeRect(108, y - 54, W - 216, 60), h1Attrs);
      y -= 74;
    } else if (raw.startsWith("## ")) {
      $(raw.slice(3)).drawInRectWithAttributes($.NSMakeRect(108, y - 42, W - 216, 48), h2Attrs);
      y -= 56;
    } else if (raw.startsWith("- ")) {
      $("•  " + raw.slice(2)).drawInRectWithAttributes($.NSMakeRect(128, y - 58, W - 246, 62), bodyAttrs);
      y -= 52;
    } else {
      $(raw).drawInRectWithAttributes($.NSMakeRect(108, y - 76, W - 216, 80), bodyAttrs);
      y -= 72;
    }
  }

  // Render first table visually if space remains
  const tables = payload.tables || [];
  if (tables.length > 0 && y > 240) {
    const tbl = tables[0];
    const cols = Math.max(1, tbl[0].length);
    const colW = Math.floor((W - 216) / cols);
    const rowH = 44;
    for (let r = 0; r < Math.min(tbl.length, 8); r++) {
      if (y - rowH < 100) break;
      if (r === 0) {
        $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.93, 0.95, 0.98, 1.0).setFill;
        $.NSRectFill($.NSMakeRect(108, y - rowH, W - 216, rowH));
      }
      $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.80, 0.84, 0.90, 1.0).setStroke;
      $.NSBezierPath.strokeRect($.NSMakeRect(108, y - rowH, W - 216, rowH));
      const cellAttrs = r === 0 ? $.NSMutableDictionary.dictionary : bodyAttrs;
      if (r === 0) {
        cellAttrs.setObjectForKey($.NSFont.boldSystemFontOfSize(20), $.NSFontAttributeName);
        cellAttrs.setObjectForKey($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.08, 0.13, 0.24, 1.0), $.NSForegroundColorAttributeName);
      }
      for (let c = 0; c < Math.min(cols, tbl[r].length); c++) {
        $(String(tbl[r][c])).drawInRectWithAttributes($.NSMakeRect(118 + c * colW, y - rowH + 8, colW - 16, rowH - 10), cellAttrs);
      }
      y -= rowH;
    }
  }

  $.NSGraphicsContext.restoreGraphicsState;
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
  png.writeToFileAtomically(outPath, true);
  return JSON.stringify({ width: 612, height: 792 });
}
"""
    with tempfile.TemporaryDirectory(prefix="rippflow-docx-img-") as tmp:
        out_png = Path(tmp) / f"page-{page}.png"
        try:
            chunk_size = 16
            total_pages = max(1, (len(paragraphs) + chunk_size - 1) // chunk_size)
            idx = max(0, min(total_pages - 1, page - 1))
            slice_paras = paragraphs[idx * chunk_size : (idx + 1) * chunk_size]
            data = json.dumps({
                "title": title,
                "paragraphs": slice_paras,
                "tables": tables if idx == 0 else [],
                "page": idx + 1,
            })
            res = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", jxa, data, str(out_png)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if res.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0:
                return {
                    "png_base64": base64.b64encode(out_png.read_bytes()).decode("ascii"),
                    "width": 612,
                    "height": 792,
                    "count": total_pages,
                }
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# DOCX Handler
# ---------------------------------------------------------------------------
def render_docx(file_path: Path, state: dict, event: str, payload: dict) -> dict:
    mode = state.get("mode", "page")
    page = int(state.get("page", 1))
    zoom = float(state.get("zoom", 1.0))

    if event == "set_mode" and isinstance(payload, dict) and payload.get("mode"):
        mode = str(payload["mode"])
    elif event == "page_change" and isinstance(payload, dict) and payload.get("page"):
        page = int(payload["page"])
    elif event == "zoom_delta" and isinstance(payload, dict) and payload.get("delta") is not None:
        zoom = max(0.5, min(2.5, round(zoom + float(payload["delta"]), 2)))
    elif event == "zoom_reset":
        zoom = 1.0

    byte_size = file_path.stat().st_size if file_path.exists() else 0
    paragraphs = []
    tables = []
    images = []
    props = {}

    with zipfile.ZipFile(file_path, "r") as zf:
        props = parse_ooxml_core_props(zf)
        if "word/document.xml" in zf.namelist():
            root = ET.fromstring(zf.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            body = root.find("w:body", ns)
            if body is not None:
                for child in body:
                    local = child.tag.split("}", 1)[-1]
                    if local == "p":
                        style_el = child.find("w:pPr/w:pStyle", ns)
                        style_val = (
                            style_el.attrib.get(f"{{{ns['w']}}}val", "")
                            if style_el is not None
                            else ""
                        )
                        is_list = child.find("w:pPr/w:numPr", ns) is not None
                        texts = [
                            t.text
                            for t in child.findall(".//w:t", ns)
                            if t.text is not None
                        ]
                        line = "".join(texts).strip()
                        if not line:
                            continue
                        m_head = re.match(r"Heading(\d)", style_val, re.IGNORECASE)
                        if m_head:
                            level = max(1, min(6, int(m_head.group(1))))
                            paragraphs.append(f"{'#' * level} {line}")
                        elif style_val.lower() == "title":
                            paragraphs.append(f"# {line}")
                        elif is_list:
                            paragraphs.append(f"- {line}")
                        else:
                            paragraphs.append(line)
                    elif local == "tbl":
                        tbl_rows = []
                        for tr in child.findall("w:tr", ns):
                            row_cells = []
                            for tc in tr.findall("w:tc", ns):
                                cell_text = " ".join(
                                    t.text.strip()
                                    for t in tc.findall(".//w:t", ns)
                                    if t.text and t.text.strip()
                                )
                                row_cells.append(cell_text)
                            if row_cells:
                                tbl_rows.append(row_cells)
                        if tbl_rows:
                            tables.append(tbl_rows)

        for name in sorted(zf.namelist()):
            if name.startswith("word/media/") and len(images) < 4:
                ext = Path(name).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                    raw = zf.read(name)
                    if len(raw) <= 2 * 1024 * 1024:
                        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else f"image/{ext[1:]}"
                        images.append(
                            (Path(name).name, mime, base64.b64encode(raw).decode("ascii"))
                        )

    word_count = sum(len(p.split()) for p in paragraphs)
    md_body = "\n\n".join(paragraphs) if paragraphs else "_No text paragraphs found in document._"

    rendered_page = None
    total_pages = 1
    if mode == "page":
        pdf_path = convert_office_to_pdf_cached(file_path)
        if pdf_path:
            rendered_page = rasterize_pdf_page_png_b64(pdf_path, max(1, page))
        if not rendered_page:
            rendered_page = render_styled_docx_page_png_macos(
                props.get("title") or file_path.name, paragraphs, tables, max(1, page)
            )
        if rendered_page and rendered_page.get("count"):
            total_pages = max(1, int(rendered_page["count"]))

    page = max(1, min(total_pages, page))
    new_state = {"mode": mode, "page": page, "zoom": zoom}

    children = [
        {
            "type": "Toolbar",
            "items": [
                {"id": "badge", "label": "DOCX", "variant": "badge"},
                {
                    "id": "mode-page",
                    "label": "Page View",
                    "active": mode == "page",
                    "event": "set_mode",
                    "payload": {"mode": "page"},
                },
                {
                    "id": "mode-doc",
                    "label": "Text",
                    "active": mode == "document",
                    "event": "set_mode",
                    "payload": {"mode": "document"},
                },
                {
                    "id": "mode-tables",
                    "label": f"Tables ({len(tables)})",
                    "active": mode == "tables",
                    "event": "set_mode",
                    "payload": {"mode": "tables"},
                },
                {
                    "id": "mode-meta",
                    "label": "Metadata",
                    "active": mode == "metadata",
                    "event": "set_mode",
                    "payload": {"mode": "metadata"},
                },
                {"id": "sp", "label": "", "variant": "spacer"},
                {
                    "id": "zoom-out",
                    "label": "−",
                    "event": "zoom_delta",
                    "payload": {"delta": -0.25},
                },
                {
                    "id": "zoom-reset",
                    "label": f"{int(round(zoom * 100))}%",
                    "event": "zoom_reset",
                },
                {
                    "id": "zoom-in",
                    "label": "+",
                    "event": "zoom_delta",
                    "payload": {"delta": 0.25},
                },
            ],
        },
        {
            "type": "Row",
            "gap": 10,
            "children": [
                {"type": "MetricCard", "label": "Pages", "value": str(total_pages), "status": "info"},
                {"type": "MetricCard", "label": "Words", "value": str(word_count), "status": "info"},
                {"type": "MetricCard", "label": "Tables", "value": str(len(tables)), "status": "success"},
                {"type": "MetricCard", "label": "Size", "value": format_size(byte_size), "status": "info"},
            ],
        },
    ]

    if mode == "page":
        if total_pages > 1:
            children.append(
                {
                    "type": "Pager",
                    "current_page": page,
                    "total_pages": total_pages,
                    "label": f"Page {page} of {total_pages}",
                    "event": "page_change",
                }
            )
        if rendered_page and rendered_page.get("png_base64"):
            children.append(
                {
                    "type": "Canvas",
                    "id": f"docx-page-{page}",
                    "title": f"{props.get('title') or file_path.name} — Page {page} of {total_pages}",
                    "width": int(rendered_page.get("width", 612)),
                    "height": int(rendered_page.get("height", 792)),
                    "zoom": zoom,
                    "png_base64": rendered_page["png_base64"],
                }
            )
        else:
            children.append(
                {
                    "type": "Markdown",
                    "title": props.get("title") or file_path.name,
                    "content": md_body,
                }
            )
    elif mode == "tables":
        if tables:
            for idx, tbl in enumerate(tables, 1):
                header = tbl[0]
                body_rows = tbl[1:] if len(tbl) > 1 else []
                children.append(
                    {
                        "type": "Table",
                        "title": f"Table {idx} ({len(body_rows)} rows)",
                        "columns": header,
                        "rows": body_rows,
                    }
                )
        else:
            children.append(
                {
                    "type": "Markdown",
                    "title": "Tables",
                    "content": "_This document does not contain any tables._",
                }
            )
    elif mode == "metadata":
        meta_rows = [
            ["File", file_path.name],
            ["Size", f"{format_size(byte_size)} ({byte_size} bytes)"],
            ["Embedded Images", str(len(images))],
        ]
        for k, v in sorted(props.items()):
            meta_rows.append([k, v])
        children.append(
            {
                "type": "Table",
                "title": "Document Properties (docProps/core.xml)",
                "columns": ["Property", "Value"],
                "rows": meta_rows,
            }
        )
    else:
        children.append(
            {
                "type": "Markdown",
                "title": props.get("title") or file_path.name,
                "content": md_body,
            }
        )
        for img_name, mime, b64 in images:
            children.append(
                {
                    "type": "Image",
                    "title": f"Embedded Media · {img_name}",
                    "base64": b64,
                    "mime_type": mime,
                    "height": 320,
                }
            )

    return {
        "id": "doc-viewer.docx",
        "title": file_path.name,
        "icon": "file",
        "state": new_state,
        "layout": {"type": "Container", "gap": 12, "padding": 14, "children": children},
    }


# ---------------------------------------------------------------------------
# PPTX Handler
# ---------------------------------------------------------------------------
def extract_pptx_slides(file_path: Path):
    slides = []
    props = {}
    with zipfile.ZipFile(file_path, "r") as zf:
        props = parse_ooxml_core_props(zf)
        slide_names = [
            n
            for n in zf.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
        ]
        slide_names.sort(key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))

        a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        for idx, name in enumerate(slide_names, 1):
            root = ET.fromstring(zf.read(name))
            lines = []
            for p in root.findall(f".//{{{a_ns}}}p"):
                runs = [
                    t.text
                    for t in p.findall(f".//{{{a_ns}}}t")
                    if t.text is not None
                ]
                text = "".join(runs).strip()
                if text:
                    lines.append(text)
            title = lines[0] if lines else f"Slide {idx}"
            body = lines[1:] if len(lines) > 1 else []

            notes_text = ""
            notes_name = f"ppt/notesSlides/notesSlide{idx}.xml"
            if notes_name in zf.namelist():
                try:
                    nroot = ET.fromstring(zf.read(notes_name))
                    nlines = [
                        t.text.strip()
                        for t in nroot.findall(f".//{{{a_ns}}}t")
                        if t.text and t.text.strip() and not t.text.strip().isdigit()
                    ]
                    notes_text = "\n".join(nlines)
                except Exception:
                    pass

            slides.append({"index": idx, "title": title, "bullets": body, "notes": notes_text})
    return slides, props


def render_pptx(file_path: Path, state: dict, event: str, payload: dict) -> dict:
    slides, props = extract_pptx_slides(file_path)
    total = max(1, len(slides))
    page = int(state.get("page", 1))
    mode = state.get("mode", "slide")
    zoom = float(state.get("zoom", 1.0))
    show_notes = bool(state.get("show_notes", True))

    if event == "page_change" and isinstance(payload, dict) and payload.get("page"):
        page = int(payload["page"])
    elif event == "set_mode" and isinstance(payload, dict) and payload.get("mode"):
        mode = str(payload["mode"])
    elif event == "zoom_delta" and isinstance(payload, dict) and payload.get("delta") is not None:
        zoom = max(0.5, min(2.0, round(zoom + float(payload["delta"]), 2)))
    elif event == "zoom_reset":
        zoom = 1.0
    elif event == "toggle_notes":
        show_notes = not show_notes

    rendered_slide = None
    if mode == "slide":
        pdf_path = convert_office_to_pdf_cached(file_path)
        if pdf_path:
            rendered_slide = rasterize_pdf_page_png_b64(pdf_path, max(1, page))
            if rendered_slide and rendered_slide.get("count"):
                total = max(1, int(rendered_slide["count"]))
        if not rendered_slide and slides:
            cur_for_render = slides[max(0, min(len(slides) - 1, page - 1))]
            rendered_slide = render_styled_slide_png_macos(
                cur_for_render["title"],
                cur_for_render["bullets"],
                cur_for_render["index"],
                len(slides),
                props.get("title") or file_path.name,
            )

    page = max(1, min(total, page))
    new_state = {"page": page, "mode": mode, "zoom": zoom, "show_notes": show_notes}
    byte_size = file_path.stat().st_size if file_path.exists() else 0

    children = [
        {
            "type": "Toolbar",
            "items": [
                {"id": "badge", "label": "PPTX DECK", "variant": "badge"},
                {
                    "id": "mode-slide",
                    "label": "Slide View",
                    "active": mode == "slide",
                    "event": "set_mode",
                    "payload": {"mode": "slide"},
                },
                {
                    "id": "mode-text",
                    "label": "Text",
                    "active": mode == "text",
                    "event": "set_mode",
                    "payload": {"mode": "text"},
                },
                {
                    "id": "mode-outline",
                    "label": f"Outline ({len(slides)})",
                    "active": mode == "outline",
                    "event": "set_mode",
                    "payload": {"mode": "outline"},
                },
                {
                    "id": "toggle-notes",
                    "label": "Hide Notes" if show_notes else "Show Notes",
                    "active": show_notes,
                    "event": "toggle_notes",
                },
                {"id": "sp", "label": "", "variant": "spacer"},
                {
                    "id": "zoom-out",
                    "label": "−",
                    "event": "zoom_delta",
                    "payload": {"delta": -0.25},
                },
                {
                    "id": "zoom-reset",
                    "label": f"{int(round(zoom * 100))}%",
                    "event": "zoom_reset",
                },
                {
                    "id": "zoom-in",
                    "label": "+",
                    "event": "zoom_delta",
                    "payload": {"delta": 0.25},
                },
            ],
        },
        {
            "type": "Row",
            "gap": 10,
            "children": [
                {"type": "MetricCard", "label": "Slides", "value": str(total), "status": "info"},
                {"type": "MetricCard", "label": "Current", "value": f"{page} / {total}", "status": "success"},
                {"type": "MetricCard", "label": "Size", "value": format_size(byte_size), "status": "info"},
            ],
        },
    ]

    if mode == "outline":
        outline_items = [
            {
                "title": f"Slide {s['index']}: {s['title']}",
                "subtitle": f"{len(s['bullets'])} point(s)"
                + (" · has speaker notes" if s["notes"] else ""),
                "badge": f"#{s['index']}",
            }
            for s in slides
        ]
        children.append(
            {
                "type": "List",
                "title": props.get("title") or f"{file_path.name} — Deck Outline",
                "items": outline_items,
            }
        )
    else:
        children.append(
            {
                "type": "Pager",
                "current_page": page,
                "total_pages": total,
                "label": f"Slide {page} of {total}",
                "event": "page_change",
            }
        )
        cur = slides[page - 1] if slides and page <= len(slides) else None
        if mode == "slide" and rendered_slide and rendered_slide.get("png_base64"):
            slide_title = cur["title"] if cur else f"Slide {page}"
            children.append(
                {
                    "type": "Canvas",
                    "id": f"pptx-slide-{page}",
                    "title": f"Slide {page} of {total} · {slide_title}",
                    "width": int(rendered_slide.get("width", 960)),
                    "height": int(rendered_slide.get("height", 540)),
                    "zoom": zoom,
                    "png_base64": rendered_slide["png_base64"],
                }
            )
            if show_notes and cur and cur["notes"]:
                children.append(
                    {
                        "type": "Markdown",
                        "title": "Speaker Notes",
                        "content": cur["notes"],
                    }
                )
        elif cur:
            bullet_md = "\n".join(f"- {b}" for b in cur["bullets"])
            slide_md = f"# {cur['title']}\n\n{bullet_md}" if bullet_md else f"# {cur['title']}"
            children.append(
                {
                    "type": "Markdown",
                    "title": f"Slide {cur['index']} · {cur['title']}",
                    "content": slide_md,
                }
            )
            if show_notes and cur["notes"]:
                children.append(
                    {
                        "type": "Markdown",
                        "title": "Speaker Notes",
                        "content": cur["notes"],
                    }
                )
        else:
            children.append(
                {
                    "type": "Markdown",
                    "title": file_path.name,
                    "content": "_No slides found in presentation._",
                }
            )

    return {
        "id": "doc-viewer.pptx",
        "title": file_path.name,
        "icon": "file",
        "state": new_state,
        "layout": {"type": "Container", "gap": 12, "padding": 14, "children": children},
    }


# ---------------------------------------------------------------------------
# PDF Handler
# ---------------------------------------------------------------------------
def inspect_pdf(file_path: Path) -> dict:
    raw = file_path.read_bytes()
    header = raw[:32].decode("latin-1", errors="ignore")
    m_ver = re.search(r"%PDF-(\d+\.\d+)", header)
    version = m_ver.group(1) if m_ver else "1.4"
    page_matches = re.findall(rb"/Type\s*/Page\b", raw)
    page_count = max(1, len(page_matches))
    encrypted = b"/Encrypt" in raw

    # Best-effort literal string extraction from PDF streams/objects
    text_chunks = []
    for m in re.finditer(rb"\(([^()\\]{2,200})\)", raw):
        s = m.group(1).decode("latin-1", errors="ignore").strip()
        if s and any(ch.isalpha() for ch in s):
            text_chunks.append(s)
        if len(text_chunks) >= 80:
            break

    return {
        "version": version,
        "page_count": page_count,
        "encrypted": encrypted,
        "byte_size": len(raw),
        "text_preview": "\n".join(text_chunks) if text_chunks else "_No uncompressed literal text found in PDF._",
    }


def rasterize_pdf_page_macos_pdfkit(file_path: Path, page: int) -> dict | None:
    """Renders any PDF page (1-indexed) to high-DPI PNG + text via built-in macOS PDFKit."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return None
    jxa = r"""
ObjC.import("PDFKit");
ObjC.import("AppKit");
function run(argv) {
  const pdfPath = argv[0];
  const pageNum = parseInt(argv[1], 10);
  const outPath = argv[2];
  const url = $.NSURL.fileURLWithPath(pdfPath);
  const doc = $.PDFDocument.alloc.initWithURL(url);
  if (!doc || doc.isNil()) { return JSON.stringify({ error: "open_failed" }); }
  const count = Number(doc.pageCount);
  if (count <= 0) { return JSON.stringify({ error: "empty" }); }
  const idx = Math.max(0, Math.min(count - 1, pageNum - 1));
  const pg = doc.pageAtIndex(idx);
  if (!pg || pg.isNil()) { return JSON.stringify({ error: "page_missing" }); }
  const bounds = pg.boundsForBox($.kPDFDisplayBoxMediaBox);
  const scale = 2.0;
  const w = Math.max(100, Math.min(2400, Math.round(bounds.size.width * scale)));
  const h = Math.max(100, Math.min(2400, Math.round(bounds.size.height * scale)));
  const img = pg.thumbnailOfSizeForBox($.NSMakeSize(w, h), $.kPDFDisplayBoxMediaBox);
  const tiff = img.TIFFRepresentation;
  const rep = $.NSBitmapImageRep.imageRepWithData(tiff);
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
  png.writeToFileAtomically(outPath, true);
  let text = "";
  try {
    const s = pg.string;
    if (s && !s.isNil()) { text = ObjC.unwrap(s); }
  } catch (e) {}
  return JSON.stringify({ count: count, width: Math.round(bounds.size.width), height: Math.round(bounds.size.height), text: text });
}
"""
    with tempfile.TemporaryDirectory(prefix="rippflow-doc-pdfkit-") as tmp:
        out_png = Path(tmp) / f"page-{page}.png"
        try:
            res = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", jxa, str(file_path), str(page), str(out_png)],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            if res.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 0:
                info = json.loads(res.stdout.strip() or "{}")
                if not info.get("error"):
                    info["png_base64"] = base64.b64encode(out_png.read_bytes()).decode("ascii")
                    return info
        except Exception:
            pass
    return None


def rasterize_pdf_page_png_b64(file_path: Path, page: int) -> dict | None:
    """Renders any PDF page to base64 PNG via macOS PDFKit, pdftoppm, or qlmanage."""
    pdfkit = rasterize_pdf_page_macos_pdfkit(file_path, page)
    if pdfkit:
        return pdfkit
    with tempfile.TemporaryDirectory(prefix="rippflow-doc-pdf-") as tmp:
        out_prefix = Path(tmp) / "page"
        if shutil.which("pdftoppm"):
            try:
                res = subprocess.run(
                    ["pdftoppm", "-png", "-r", "150", "-f", str(page), "-l", str(page), str(file_path), str(out_prefix)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if res.returncode == 0:
                    pngs = sorted(Path(tmp).glob("page*.png"))
                    if pngs:
                        return {"png_base64": base64.b64encode(pngs[0].read_bytes()).decode("ascii")}
            except subprocess.TimeoutExpired:
                pass
        if page == 1 and sys.platform == "darwin" and shutil.which("qlmanage"):
            try:
                res = subprocess.run(
                    ["qlmanage", "-t", "-s", "1200", "-o", tmp, str(file_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
                if res.returncode == 0:
                    pngs = sorted(Path(tmp).glob("*.png"))
                    if pngs:
                        return {"png_base64": base64.b64encode(pngs[0].read_bytes()).decode("ascii")}
            except subprocess.TimeoutExpired:
                pass
    return None


def render_pdf(file_path: Path, state: dict, event: str, payload: dict) -> dict:
    meta = inspect_pdf(file_path)
    total = meta["page_count"]
    page = int(state.get("page", 1))
    zoom = float(state.get("zoom", 1.0))
    mode = state.get("mode", "page")
    last_click = state.get("last_click")

    if event == "page_change" and isinstance(payload, dict) and payload.get("page"):
        page = int(payload["page"])
    elif event == "set_mode" and isinstance(payload, dict) and payload.get("mode"):
        mode = str(payload["mode"])
    elif event == "zoom_delta" and isinstance(payload, dict) and payload.get("delta") is not None:
        zoom = max(0.5, min(2.5, round(zoom + float(payload["delta"]), 2)))
    elif event == "zoom_reset":
        zoom = 1.0
    elif event == "canvas_click" and isinstance(payload, dict):
        last_click = f"Clicked ({payload.get('x', 0):.0f}, {payload.get('y', 0):.0f})"

    rendered = rasterize_pdf_page_png_b64(file_path, max(1, page))
    if rendered and rendered.get("count"):
        total = max(1, int(rendered["count"]))

    page = max(1, min(total, page))
    new_state = {"page": page, "zoom": zoom, "mode": mode, "last_click": last_click}

    children = [
        {
            "type": "Toolbar",
            "items": [
                {"id": "badge", "label": f"PDF v{meta['version']}", "variant": "badge"},
                {
                    "id": "mode-page",
                    "label": "Page Canvas",
                    "active": mode == "page",
                    "event": "set_mode",
                    "payload": {"mode": "page"},
                },
                {
                    "id": "mode-text",
                    "label": "Extracted Text",
                    "active": mode == "text",
                    "event": "set_mode",
                    "payload": {"mode": "text"},
                },
                {"id": "sp", "label": "", "variant": "spacer"},
                {
                    "id": "zoom-out",
                    "label": "−",
                    "event": "zoom_delta",
                    "payload": {"delta": -0.25},
                },
                {
                    "id": "zoom-reset",
                    "label": f"{int(round(zoom * 100))}%",
                    "event": "zoom_reset",
                },
                {
                    "id": "zoom-in",
                    "label": "+",
                    "event": "zoom_delta",
                    "payload": {"delta": 0.25},
                },
            ],
        },
        {
            "type": "Row",
            "gap": 10,
            "children": [
                {"type": "MetricCard", "label": "Pages", "value": str(total), "status": "info"},
                {"type": "MetricCard", "label": "PDF Version", "value": meta["version"], "status": "info"},
                {
                    "type": "MetricCard",
                    "label": "Security",
                    "value": "Encrypted" if meta["encrypted"] else "Unencrypted",
                    "status": "warning" if meta["encrypted"] else "success",
                },
                {"type": "MetricCard", "label": "Size", "value": format_size(meta["byte_size"]), "status": "info"},
            ],
        },
        {
            "type": "Pager",
            "current_page": page,
            "total_pages": total,
            "label": f"Page {page} of {total}",
            "event": "page_change",
        },
    ]

    if mode == "text":
        page_text = (rendered.get("text") or "").strip() if rendered else ""
        children.append(
            {
                "type": "Markdown",
                "title": f"{file_path.name} — Page {page} Text",
                "content": page_text or meta["text_preview"],
            }
        )
    else:
        png_b64 = rendered.get("png_base64") if rendered else None
        page_w = int(rendered.get("width", 612)) if rendered else 612
        page_h = int(rendered.get("height", 792)) if rendered else 792
        canvas_node = {
            "type": "Canvas",
            "id": f"pdf-page-{page}",
            "title": f"{file_path.name} — Page {page}" + (f" · {last_click}" if last_click else ""),
            "width": page_w,
            "height": page_h,
            "zoom": zoom,
            "interactive": True,
            "event": "canvas_click",
        }
        if png_b64:
            canvas_node["png_base64"] = png_b64
        else:
            canvas_node["width"] = 320
            canvas_node["height"] = 400
            canvas_node["rgba_base64"] = make_rgba_banner(
                320, 400, (56, 189, 248), (24, 28, 38)
            )
        children.append(canvas_node)

    return {
        "id": "doc-viewer.pdf",
        "title": file_path.name,
        "icon": "file",
        "state": new_state,
        "layout": {"type": "Container", "gap": 12, "padding": 14, "children": children},
    }


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "Empty stdin request"}), file=sys.stderr)
        return 1
    req = json.loads(raw)
    file_path = Path(req.get("path", ""))
    if not file_path.is_file():
        print(json.dumps({"error": f"File not found: {file_path}"}), file=sys.stderr)
        return 1

    state = req.get("state") or {}
    event = req.get("event") or ""
    payload = req.get("payload") or {}
    ext = file_path.suffix.lower().lstrip(".")

    if ext == "docx":
        spec = render_docx(file_path, state, event, payload)
    elif ext == "pptx":
        spec = render_pptx(file_path, state, event, payload)
    elif ext == "pdf":
        spec = render_pdf(file_path, state, event, payload)
    else:
        print(json.dumps({"error": f"Unsupported extension: .{ext}"}), file=sys.stderr)
        return 1

    sys.stdout.write(json.dumps(spec) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
