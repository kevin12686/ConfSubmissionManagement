import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import fitz


HEADER_PADDING = 10
HEADER_GAP = 6
HEADER_TO_CONTENT_MARGIN = 8
BODY_FONT_SIZE = 9
BODY_LINE_HEIGHT = 12
LABEL_FONT_SIZE = 7
AUTHOR_BADGE_WIDTH = 24
DEFAULT_CONTENT_FRACTION = 1 / 3
MAX_CONTENT_FRACTION = 0.60
BLANK_PIXEL_THRESHOLD = 252
VERIFICATION_FONT_ALIAS = "verification_unicode"
VERIFICATION_FONT_NAME = "cjk"


def generate_verification_image(
    pdf_path,
    extracted_title,
    extracted_authors,
    source_label,
    target_dir,
    author_names,
):
    """Render one collision-safe review image for every extraction source."""
    pdf_path = Path(pdf_path)
    target_dir = Path(target_dir)
    source_slug = _safe_source_slug(source_label)
    output_path = target_dir / f"{pdf_path.stem}-{source_slug}.png"
    authors = list(author_names)
    missing_authors = []

    with fitz.open(pdf_path) as source_document:
        if not source_document.page_count:
            raise ValueError("PDF has no pages.")

        source_page = source_document[0]
        source_rect = source_page.rect
        search_clip = fitz.Rect(
            source_rect.x0,
            source_rect.y0,
            source_rect.x1,
            source_rect.y0 + (source_rect.height * MAX_CONTENT_FRACTION),
        )
        title_rects = _find_text_rects(source_page, extracted_title, search_clip)
        author_rects = []
        for author in authors:
            matches = _find_author_text_rects(source_page, author, search_clip)
            author_rects.append(matches)
            if not matches:
                missing_authors.append(author)
        source_clip = _content_clip(source_rect, title_rects, author_rects)

        header_layout = _build_header_layout(
            source_rect.width,
            pdf_path.name,
            extracted_title,
            authors,
            source_label,
        )
        header_height = header_layout["height"]
        top_blank_height = _safe_top_blank_height(source_page, source_clip)
        source_offset = _source_offset(header_height, top_blank_height)

        target_dir.mkdir(parents=True, exist_ok=True)
        output_document = fitz.open()
        try:
            output_page = output_document.new_page(
                width=source_rect.width,
                height=source_offset + source_clip.height,
            )
            output_page.show_pdf_page(
                fitz.Rect(
                    0,
                    source_offset,
                    source_rect.width,
                    source_offset + source_clip.height,
                ),
                source_document,
                0,
                clip=source_clip,
            )
            _draw_header(output_page, header_layout, source_rect.width)
            _draw_title_evidence(output_page, title_rects, source_offset, source_clip)
            _draw_author_evidence(output_page, author_rects, source_offset, source_clip)

            pixmap = output_page.get_pixmap(dpi=300, alpha=False)
            pixmap.save(output_path)
        finally:
            output_document.close()

    return output_path, missing_authors


def _safe_source_slug(value):
    slug = re.sub(r"[^A-Za-z0-9.-]+", "_", str(value or "").strip()).strip("._")
    return slug.lower() or "extraction"


def _safe_top_blank_height(page, clip):
    """Return the visibly blank top band using a conservative grayscale scan."""
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(1, 1),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=True,
        clip=clip,
    )
    samples = pixmap.samples
    for row_index in range(pixmap.height):
        start = row_index * pixmap.stride
        row = samples[start : start + pixmap.width]
        if row and min(row) < BLANK_PIXEL_THRESHOLD:
            return row_index * (clip.height / pixmap.height)
    return clip.height


def _source_offset(header_height, top_blank_height):
    return max(
        0,
        header_height + HEADER_TO_CONTENT_MARGIN - top_blank_height,
    )


def _content_clip(source_rect, title_rects, author_rect_groups):
    default_bottom = source_rect.y0 + (source_rect.height * DEFAULT_CONTENT_FRACTION)
    maximum_bottom = source_rect.y0 + (source_rect.height * MAX_CONTENT_FRACTION)
    matched_rects = list(title_rects)
    for rects in author_rect_groups:
        matched_rects.extend(rects)
    evidence_bottom = max((rect.y1 for rect in matched_rects), default=default_bottom)
    bottom = min(max(default_bottom, evidence_bottom + 18), maximum_bottom)
    return fitz.Rect(source_rect.x0, source_rect.y0, source_rect.x1, bottom)


def _build_header_layout(page_width, filename, title, authors, source_label):
    content_width = max(page_width - (HEADER_PADDING * 2), 120)
    filename_lines = _wrap_text(filename, content_width, BODY_FONT_SIZE)
    title_lines = _wrap_text(
        title or "Missing extracted title",
        content_width,
        BODY_FONT_SIZE,
    )
    author_lines = [
        _wrap_text(author, content_width - AUTHOR_BADGE_WIDTH - 8, BODY_FONT_SIZE)
        for author in authors
    ]
    if not author_lines:
        author_lines = [["Missing extracted authors"]]

    y = HEADER_PADDING
    source_badge_height = 19
    y += source_badge_height + 5
    filename_top = y
    y += max(len(filename_lines), 1) * BODY_LINE_HEIGHT
    y += HEADER_GAP

    title_label_top = y
    y += LABEL_FONT_SIZE + 4
    title_top = y
    y += max(len(title_lines), 1) * BODY_LINE_HEIGHT
    y += HEADER_GAP

    authors_label_top = y
    y += LABEL_FONT_SIZE + 4
    authors_top = y
    for lines in author_lines:
        y += max(len(lines), 1) * BODY_LINE_HEIGHT + 3
    y += HEADER_PADDING

    return {
        "height": y,
        "source_label": f"REVIEW SAMPLE | {source_label.upper()}",
        "filename_lines": filename_lines,
        "filename_top": filename_top,
        "title_lines": title_lines,
        "title_label_top": title_label_top,
        "title_top": title_top,
        "author_lines": author_lines,
        "authors_label_top": authors_label_top,
        "authors_top": authors_top,
    }


def _draw_header(page, layout, page_width):
    page.insert_font(
        fontname=VERIFICATION_FONT_ALIAS,
        fontbuffer=_verification_font().buffer,
    )
    page.draw_rect(
        fitz.Rect(0, 0, page_width, layout["height"]),
        color=(0.72, 0.76, 0.80),
        fill=(0.96, 0.97, 0.98),
        width=0.6,
    )
    badge_width = min(
        _text_width(layout["source_label"], BODY_FONT_SIZE) + 18,
        page_width - (HEADER_PADDING * 2),
    )
    badge_rect = fitz.Rect(
        HEADER_PADDING,
        HEADER_PADDING,
        HEADER_PADDING + badge_width,
        HEADER_PADDING + 19,
    )
    page.draw_rect(
        badge_rect,
        color=(0.72, 0.10, 0.12),
        fill=(1, 0.95, 0.95),
        width=0.9,
    )
    page.insert_text(
        fitz.Point(badge_rect.x0 + 8, badge_rect.y0 + 13),
        layout["source_label"],
        fontname=VERIFICATION_FONT_ALIAS,
        fontsize=BODY_FONT_SIZE,
        color=(0.58, 0.05, 0.07),
    )

    _draw_lines(
        page,
        layout["filename_lines"],
        HEADER_PADDING,
        layout["filename_top"],
        BODY_FONT_SIZE,
        (0.20, 0.24, 0.29),
    )

    _draw_section_label(page, "EXTRACTED TITLE", layout["title_label_top"])
    _draw_lines(
        page,
        layout["title_lines"],
        HEADER_PADDING,
        layout["title_top"],
        BODY_FONT_SIZE,
        (0.05, 0.25, 0.72),
    )

    _draw_section_label(page, "EXTRACTED AUTHORS", layout["authors_label_top"])
    y = layout["authors_top"]
    for index, lines in enumerate(layout["author_lines"], start=1):
        if lines == ["Missing extracted authors"]:
            _draw_lines(
                page,
                lines,
                HEADER_PADDING,
                y,
                BODY_FONT_SIZE,
                (0.64, 0.08, 0.10),
            )
        else:
            badge_rect = fitz.Rect(
                HEADER_PADDING,
                y - 1,
                HEADER_PADDING + AUTHOR_BADGE_WIDTH,
                y + BODY_LINE_HEIGHT - 1,
            )
            page.draw_rect(
                badge_rect,
                color=(0.07, 0.38, 0.20),
                fill=(0.88, 0.97, 0.90),
                width=0.8,
            )
            page.insert_text(
                fitz.Point(badge_rect.x0 + 5, badge_rect.y0 + 9),
                f"A{index}",
                fontname=VERIFICATION_FONT_ALIAS,
                fontsize=7,
                color=(0.04, 0.31, 0.15),
            )
            _draw_lines(
                page,
                lines,
                HEADER_PADDING + AUTHOR_BADGE_WIDTH + 8,
                y,
                BODY_FONT_SIZE,
                (0.12, 0.25, 0.17),
            )
        y += max(len(lines), 1) * BODY_LINE_HEIGHT + 3

    page.draw_line(
        fitz.Point(0, layout["height"] - 1),
        fitz.Point(page_width, layout["height"] - 1),
        color=(0.25, 0.31, 0.38),
        width=1,
    )


def _draw_section_label(page, label, top):
    page.insert_text(
        fitz.Point(HEADER_PADDING, top + LABEL_FONT_SIZE),
        label,
        fontname=VERIFICATION_FONT_ALIAS,
        fontsize=LABEL_FONT_SIZE,
        color=(0.30, 0.35, 0.41),
    )


def _draw_lines(page, lines, x, top, font_size, color):
    for index, line in enumerate(lines):
        page.insert_text(
            fitz.Point(x, top + font_size + (index * BODY_LINE_HEIGHT)),
            line,
            fontname=VERIFICATION_FONT_ALIAS,
            fontsize=font_size,
            color=color,
        )


def _draw_title_evidence(page, rects, source_offset, source_clip):
    for source_rect in rects:
        rect = _translated_rect(source_rect, source_offset, source_clip)
        page.draw_rect(
            rect,
            color=None,
            fill=(1, 0.95, 0),
            fill_opacity=0.42,
            overlay=True,
        )
        page.draw_line(
            fitz.Point(rect.x0, rect.y1 + 0.6),
            fitz.Point(rect.x1, rect.y1 + 0.6),
            color=(0.05, 0.28, 0.82),
            width=1.3,
            overlay=True,
        )


def _draw_author_evidence(page, author_rect_groups, source_offset, source_clip):
    for rects in author_rect_groups:
        for source_rect in rects:
            rect = _translated_rect(source_rect, source_offset, source_clip)
            page.draw_rect(
                rect,
                color=(0.05, 0.45, 0.20),
                fill=(0.25, 0.95, 0.35),
                fill_opacity=0.30,
                width=1.1,
                overlay=True,
            )
            page.draw_line(
                fitz.Point(rect.x0, rect.y1 + 0.6),
                fitz.Point(rect.x1, rect.y1 + 0.6),
                color=(0.03, 0.35, 0.15),
                width=1.2,
                overlay=True,
            )


def _translated_rect(source_rect, source_offset, source_clip):
    return fitz.Rect(
        source_rect.x0 - source_clip.x0,
        source_offset + source_rect.y0 - source_clip.y0,
        source_rect.x1 - source_clip.x0,
        source_offset + source_rect.y1 - source_clip.y0,
    )


def _find_text_rects(page, text, clip):
    return _find_normalized_text_rects(page, text, clip)


def _find_author_text_rects(page, text, clip):
    text = unicodedata.normalize("NFC", str(text or "").strip())
    target_words = re.findall(r"\S+", text)
    if not target_words:
        return []

    words = [
        word
        for word in page.get_text("words", clip=clip, sort=True)
        if str(word[4]).strip()
    ]
    raw_lines = _raw_line_characters(page, clip)
    matches = []
    word_count = len(target_words)
    for start in range(len(words) - word_count + 1):
        candidate_words = words[start : start + word_count]
        adjusted_words = []
        for index, (word, target_word) in enumerate(
            zip(candidate_words, target_words, strict=True)
        ):
            rect = _author_word_match_rect(
                word,
                target_word,
                raw_lines,
                allow_leading_marker=index == 0,
                allow_trailing_marker=index == word_count - 1,
            )
            if rect is None:
                break
            adjusted_words.append(
                (rect.x0, rect.y0, rect.x1, rect.y1, *word[4:])
            )
        else:
            matches.extend(_merge_word_rects(adjusted_words))
    return matches


def _find_normalized_text_rects(
    page,
    text,
    clip,
):
    text = (text or "").strip()
    if not text:
        return []

    target = _normalized_match_text(text)
    if not target:
        return []

    words = [
        word
        for word in page.get_text("words", clip=clip, sort=True)
        if _normalized_match_text(word[4])
    ]
    matches = []
    for start in range(len(words)):
        combined = ""
        matched = []
        for word in words[start:]:
            combined += _normalized_match_text(word[4])
            matched.append(word)
            if combined == target:
                matches.extend(_merge_word_rects(matched))
                break
            if not target.startswith(combined):
                break
    return matches


def _merge_word_rects(words):
    merged = []
    current = None
    current_line = None
    for word in words:
        rect = fitz.Rect(word[:4])
        line_key = (word[5], word[6])
        if current is not None and line_key == current_line:
            current.include_rect(rect)
        else:
            if current is not None:
                merged.append(current)
            current = rect
            current_line = line_key
    if current is not None:
        merged.append(current)
    return merged


def _raw_line_characters(page, clip):
    lines = {}
    raw_text = page.get_text("rawdict", clip=clip, sort=False)
    for block_index, block in enumerate(raw_text.get("blocks", [])):
        block_number = block.get("number", block_index)
        for line_index, line in enumerate(block.get("lines", [])):
            lines[(block_number, line_index)] = [
                character
                for span in line.get("spans", [])
                for character in span.get("chars", [])
            ]
    return lines


def _author_word_match_rect(
    word,
    target_word,
    raw_lines,
    *,
    allow_leading_marker,
    allow_trailing_marker,
):
    word_rect = fitz.Rect(word[:4])
    characters = raw_lines.get((word[5], word[6]), [])
    word_characters = []
    for character in characters:
        character_rect = fitz.Rect(character["bbox"])
        character_center = fitz.Point(
            (character_rect.x0 + character_rect.x1) / 2,
            (character_rect.y0 + character_rect.y1) / 2,
        )
        if word_rect.contains(character_center):
            word_characters.append((character["c"], character_rect))

    units = _normalized_author_character_units(word_characters)
    candidate = "".join(character for character, _rect in units)
    target = unicodedata.normalize("NFC", target_word)
    if not candidate or not target:
        return None

    search_start = 0
    while True:
        match_start = candidate.find(target, search_start)
        if match_start < 0:
            return None
        match_end = match_start + len(target)
        prefix = candidate[:match_start]
        suffix = candidate[match_end:]
        if (
            (not prefix or allow_leading_marker and _is_external_author_prefix(prefix))
            and (
                not suffix
                or allow_trailing_marker and _is_external_author_suffix(suffix)
            )
        ):
            matched_rects = [rect for _character, rect in units[match_start:match_end]]
            if not matched_rects:
                return None
            matched_rect = fitz.Rect(matched_rects[0])
            for character_rect in matched_rects[1:]:
                matched_rect.include_rect(character_rect)
            return matched_rect
        search_start = match_start + 1


def _normalized_author_character_units(characters):
    clusters = []
    for character, rect in characters:
        if not character:
            continue
        if clusters and unicodedata.category(character).startswith("M"):
            clusters[-1][0] += character
            clusters[-1][1].include_rect(rect)
        else:
            clusters.append([character, fitz.Rect(rect)])

    units = []
    for cluster_text, cluster_rect in clusters:
        for character in unicodedata.normalize("NFC", cluster_text):
            units.append((character, fitz.Rect(cluster_rect)))
    return units


def _is_external_author_prefix(value):
    return all(not character.isalnum() for character in value)


def _is_external_author_suffix(value):
    return all(not character.isalpha() for character in value)


def _normalized_match_text(value):
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _wrap_text(text, max_width, font_size):
    text = re.sub(r"\s+", " ", str(text or "").strip())
    if not text:
        return [""]

    lines = []
    current = ""
    for token in text.split(" "):
        candidate = token if not current else f"{current} {token}"
        if _text_width(candidate, font_size) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if _text_width(token, font_size) <= max_width:
            current = token
            continue
        pieces = _split_long_token(token, max_width, font_size)
        lines.extend(pieces[:-1])
        current = pieces[-1]
    if current:
        lines.append(current)
    return lines or [""]


def _split_long_token(token, max_width, font_size):
    pieces = []
    current = ""
    for character in token:
        candidate = current + character
        if current and _text_width(candidate, font_size) > max_width:
            pieces.append(current)
            current = character
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [token]


def _text_width(text, font_size):
    text = str(text)
    font = _verification_font()
    unsupported = sorted(
        {
            character
            for character in text
            if not character.isspace() and not font.has_glyph(ord(character))
        }
    )
    if unsupported:
        codepoints = ", ".join(f"U+{ord(character):04X}" for character in unsupported)
        raise ValueError(
            "Verification evidence cannot represent all header characters with "
            f"the bundled Unicode font: {codepoints}."
        )
    return font.text_length(text, fontsize=font_size)


@lru_cache(maxsize=1)
def _verification_font():
    """Return MuPDF's bundled Unicode fallback; initialization failures are fatal."""
    try:
        return fitz.Font(fontname=VERIFICATION_FONT_NAME)
    except Exception as exc:
        raise RuntimeError(
            "The bundled Unicode font required for verification evidence is unavailable."
        ) from exc
