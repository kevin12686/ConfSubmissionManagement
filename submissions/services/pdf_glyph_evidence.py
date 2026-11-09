"""Read-only glyph evidence for the isolated PDF comparison worker.

Public API: inspect_pdf_pair(before: bytes, after: bytes) -> (before, after).
Each result contains words/tokens/pages/controls plus glyphs, inventory,
uncertain_regions, coverage, coverage_reasons and evidence_summary. No Django, storage or file
selection dependencies. Resource/time limits belong to the worker parent.

Font identities are bound to retained MuPDF PDF font descriptors, never names.
Exact outline keys identify appearance, not Unicode or formula equivalence.
Unknown glyphs remain warnings even when the two inputs are identical.

Tokens have one char_meta entry per logical code point, referencing an occurrence
in glyphs. A ligature can therefore reference one glyph several times. Shared
outline_key values prove only exact normalized outlines, not text meaning.
Eligible unresolved shared outlines emit one U+FFFC token with opaque_key and
original raw text. text_identity_supported permits identity alignment, including
opaque shape alignment, independently of role-only warnings; it is not a claim
of Unicode meaning, complete structure coverage or publication correctness.
Uncertain vector regions supply left/right_token_index and token_key anchors;
candidate_values are review hints only, never accepted text or equality evidence.
Table context_key values are coordinate-free JSON; ambiguous identity is warned.
"""

import io
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from contextlib import ExitStack
from fractions import Fraction

import fitz
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.basePen import BasePen
from pypdf import PdfReader
from pypdf.generic import (ArrayObject, BooleanObject, ByteStringObject, DictionaryObject,
                           FloatObject, IndirectObject, NameObject, NullObject, NumberObject,
                           StreamObject, TextStringObject)

from submissions.services.pdf_text_evidence import STANDARD_ENCODINGS, STANDARD_FONTS, unicode_map


class GlyphEvidenceError(ValueError):
    """Known input failure, safe for the worker's structured error response."""


_TOKENS = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_M = fitz.mupdf
MAX_NATIVE_ITEMS = 500_000
MAX_CANDIDATE_OUTLINES = 100_000
MAX_COLLECTION_FACES = 64
MAX_TABLE_OWNERSHIP_WORK = 2_000_000
MAX_RENDER_SOURCE_NODES = 100_000
MAX_RENDER_SOURCE_BYTES = 64 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _valid_unicode(value):
    return bool(value) and not any(c == "\ufffd" or unicodedata.category(c) in {"Cs", "Co", "Cn"} for c in value)


def _invisible(value):
    return bool(value) and all(c.isspace() or unicodedata.category(c).startswith("C") for c in value)


class _RenderSource:
    """Exact, bounded render-input closure; absence of proof never means equal."""

    def __init__(self, reader):
        # The tolerant reader used for local text recovery can repair invalid
        # objects differently from MuPDF (notably duplicate dictionary keys).
        # Such objects cannot participate in exact render-input proof.
        self.reader = None
        if reader is not None:
            try:
                self.reader = PdfReader(reader.stream, strict=True)
            except Exception:
                pass
        self.cache = {}
        self.active = set()
        self.nodes = self.bytes = 0

    def digest(self, value, depth=0):
        self.nodes += 1
        if depth > 64 or self.nodes > MAX_RENDER_SOURCE_NODES:
            raise ValueError("Render source work limit")
        if isinstance(value, IndirectObject):
            key = (value.idnum, value.generation)
            if key in self.active:
                raise ValueError("Cyclic render source")
            if key not in self.cache:
                self.active.add(key)
                try:
                    resolved = value.get_object()
                    if resolved is None or isinstance(resolved, NullObject):
                        raise ValueError("Unresolved render source")
                    self.cache[key] = self.digest(resolved, depth + 1)
                finally:
                    self.active.remove(key)
            return self.cache[key]
        if isinstance(value, DictionaryObject):
            parts = [(str(key), self.digest(value.raw_get(key), depth + 1)) for key in sorted(value)]
            payload = json.dumps(["dictionary", parts], ensure_ascii=True).encode("ascii")
            if isinstance(value, StreamObject):
                if "/F" in value or not isinstance(value._data, bytes):
                    raise ValueError("External or unsupported stream")
                self.bytes += len(value._data)
                if self.bytes > MAX_RENDER_SOURCE_BYTES:
                    raise ValueError("Render source byte limit")
                payload += b"\x00stream\x00" + hashlib.sha256(value._data).digest()
        elif isinstance(value, ArrayObject):
            payload = json.dumps(["array", [self.digest(v, depth + 1) for v in value]]).encode("ascii")
        elif isinstance(value, (TextStringObject, ByteStringObject)):
            payload = b"string\x00" + value.original_bytes
        elif isinstance(value, NameObject):
            payload = b"name\x00" + str(value).encode("utf-8")
        elif isinstance(value, BooleanObject):
            payload = b"true" if value.value else b"false"
        elif isinstance(value, (FloatObject, NumberObject)):
            if not math.isfinite(value):
                raise ValueError("Nonfinite render source")
            payload = ("number:" + str(Fraction(value))).encode("ascii")
        elif isinstance(value, NullObject):
            payload = b"null"
        else:
            raise ValueError("Unsupported render source object")
        self.bytes += len(payload)
        if self.bytes > MAX_RENDER_SOURCE_BYTES:
            raise ValueError("Render source byte limit")
        return _digest(payload)

    def _destination(self, value):
        value = value.get_object()
        if isinstance(value, (NameObject, TextStringObject, ByteStringObject)):
            return value
        if not isinstance(value, ArrayObject) or len(value) < 2 or not isinstance(value[0], IndirectObject):
            raise ValueError("Unsupported link destination")
        target = value[0]
        pages = self.reader.pages
        if len(pages) > 100:
            raise ValueError("Link destination page limit")
        matches = [index for index, page in enumerate(pages)
                   if page.indirect_reference == target]
        if len(matches) != 1:
            raise ValueError("Unresolved link destination page")
        # Navigation page references are not appearance-resource recursion.
        # Retain their exact page identity and destination arguments instead.
        return ArrayObject([NumberObject(matches[0]), *value[1:]])

    def _link_annotations(self, page):
        annotations = page["/Annots"]
        if not isinstance(annotations, ArrayObject) or len(annotations) > 1000:
            raise ValueError("Unsupported annotation list")
        result = ArrayObject()
        for reference in annotations:
            annotation = reference.get_object()
            if (not isinstance(annotation, DictionaryObject) or annotation.get("/Subtype") != "/Link"
                    or "/AA" in annotation):
                raise ValueError("Annotation appearance is outside source proof")
            item = DictionaryObject(annotation)
            if "/P" in annotation:
                if annotation.raw_get("/P") != page.indirect_reference:
                    raise ValueError("Unproved annotation page backlink")
                # The enclosing page is already hashed, including its geometry
                # and resources. Only this validated ownership backlink is cut.
                item.pop(NameObject("/P"))
            if "/Dest" in annotation:
                item[NameObject("/Dest")] = self._destination(annotation.raw_get("/Dest"))
            if "/A" in annotation:
                action = annotation["/A"]
                if (not isinstance(action, DictionaryObject) or action.get("/S") not in {"/URI", "/GoTo"}
                        or "/Next" in action):
                    raise ValueError("Unsupported link action")
                if action.get("/S") == "/GoTo":
                    action = DictionaryObject(action)
                    action[NameObject("/D")] = self._destination(action.raw_get("/D"))
                item[NameObject("/A")] = action
            # Keep AP/AS, borders, color, opacity, flags, optional content, and
            # all other properties. Cycles and external streams still fail.
            result.append(item)
        return result

    def page_key(self, number):
        if self.reader is None:
            raise ValueError("Strict render source inspection is unavailable")
        page = self.reader.pages[number]
        root = DictionaryObject()
        # PdfReader.pages resolves inherited Resources, MediaBox, CropBox and
        # Rotate. Include other page properties conservatively, but not the
        # page-tree backlink or non-rendering metadata.
        for key in page:
            if key not in {"/Parent", "/Metadata"}:
                root[NameObject(key)] = page.raw_get(key)
        if page.get("/Annots"):
            root[NameObject("/Annots")] = self._link_annotations(page)
        # Explicit null is also absent for inheritable page attributes. pypdf
        # leaves this spelling unflattened; never hash null instead of its
        # actual inherited rendering value.
        for key in ("/Resources", "/MediaBox", "/CropBox", "/Rotate"):
            node, seen = page, set()
            for _ in range(64):
                if id(node) in seen:
                    raise ValueError("Cyclic page inheritance")
                seen.add(id(node))
                if key in node and not isinstance(node[key], NullObject):
                    root[NameObject(key)] = node.raw_get(key)
                    break
                parent = node.get("/Parent")
                if parent is None:
                    root.pop(NameObject(key), None)
                    break
                node = parent.get_object()
                if not isinstance(node, DictionaryObject):
                    raise ValueError("Invalid page inheritance")
            else:
                raise ValueError("Page inheritance depth limit")
        if "/MediaBox" not in root:
            raise ValueError("Missing page geometry")
        catalog = self.reader.trailer["/Root"]
        for key in ("/OCProperties", "/OutputIntents", "/Version", "/Extensions"):
            if key in catalog:
                root[NameObject(key)] = catalog.raw_get(key)
        return self.digest(root)


class _Face:
    """One parsed embedded face; outlines are drawn only for used glyph IDs."""

    def __init__(self, data, face):
        self.key = f"{_digest(data)}:{face}"
        self.font = None
        self.outlines = {}
        self.meanings = defaultdict(set)
        self.ligatures = defaultdict(set)
        self.script_names = set()
        self.problem = None
        try:
            self.font = TTFont(io.BytesIO(data), fontNumber=face, lazy=True)
            self.order = self.font.getGlyphOrder()
            self.glyphset = self.font.getGlyphSet()
            self.units = self.font["head"].unitsPerEm
            if not self.units or "fvar" in self.font:
                raise ValueError("Unresolved variable font instance")
            # All Unicode cmaps contribute, so conflicting tables cannot be
            # hidden by getBestCmap's platform preference.
            for table in self.font["cmap"].tables if "cmap" in self.font else []:
                if table.isUnicode():
                    for code, name in table.cmap.items():
                        if 0 <= code <= 0x10ffff and _valid_unicode(chr(code)):
                            self.meanings[name].add(chr(code))
            if "GSUB" in self.font:
                table = self.font["GSUB"].table
                features = table.FeatureList.FeatureRecord if table.FeatureList else []
                original = {name: set(values) for name, values in self.meanings.items()}
                for feature in features:
                    if feature.FeatureTag not in {"ssty", "liga", "rlig", "dlig"}:
                        continue
                    for index in feature.Feature.LookupListIndex:
                        lookup = table.LookupList.Lookup[index]
                        for sub in lookup.SubTable:
                            sub = getattr(sub, "ExtSubTable", sub)
                            if feature.FeatureTag == "ssty":
                                pairs = list(getattr(sub, "alternates", {}).items())
                                pairs += [(key, [value]) for key, value in getattr(sub, "mapping", {}).items()]
                                for base, alternates in pairs:
                                    for alternate in alternates:
                                        self.meanings[alternate].update(original.get(base, ()))
                                        self.script_names.add(alternate)
                            else:
                                for first, ligatures in getattr(sub, "ligatures", {}).items():
                                    for ligature in ligatures:
                                        parts = [original.get(name, set()) for name in [first, *ligature.Component]]
                                        if all(len(part) == 1 for part in parts):
                                            value = "".join(next(iter(part)) for part in parts)
                                            self.ligatures[ligature.LigGlyph].add(value)
            if "MATH" in self.font:
                variants = self.font["MATH"].table.MathVariants
                if variants:
                    for axis in ("Vert", "Horiz"):
                        coverage = getattr(variants, axis + "GlyphCoverage", None)
                        constructions = getattr(variants, axis + "GlyphConstruction", [])
                        if coverage:
                            for base, construction in zip(coverage.glyphs, constructions):
                                for variant in construction.MathGlyphVariantRecord:
                                    self.meanings[variant.VariantGlyph].update(self.meanings.get(base, ()))
                                    self.script_names.add(variant.VariantGlyph)
        except Exception:
            self.problem = "Embedded font outlines or Unicode tables could not be inspected."

    def glyph(self, gid):
        if gid in self.outlines:
            return self.outlines[gid]
        result = {"outline_key": None, "empty": None, "candidates": [], "ligatures": [], "recording": None}
        if self.problem:
            result["error"] = self.problem
        elif gid >= len(self.order):
            result["error"] = "The native glyph ID is outside the embedded font's glyph order."
        if self.problem is None and 0 < gid < len(self.order):
            name = self.order[gid]
            result["candidates"] = sorted(self.meanings.get(name, ()))
            result["ligatures"] = sorted(self.ligatures.get(name, ()))
            try:
                pen = DecomposingRecordingPen(self.glyphset, skipMissingComponents=False)
                self.glyphset[name].draw(pen)
                result["empty"] = not pen.value
                if pen.value:
                    # Rational font-unit coordinates: no rounding, raster
                    # similarity, contour fitting or approximate acceptance.
                    value = tuple((op, tuple(None if p is None else tuple(
                        str(Fraction(v) / self.units) for v in p) for p in points)) for op, points in pen.value)
                    result["outline_key"] = _digest(repr(value).encode("ascii"))
                    result["recording"] = pen.value
            except Exception:
                result["error"] = "The used glyph outline could not be inspected."
        self.outlines[gid] = result
        return result

    def close(self):
        if self.font:
            self.font.close()


class _Resources:
    def __init__(self, document, data, faces):
        self.document = document
        self.reader = PdfReader(io.BytesIO(data))
        self.faces = faces
        self.descriptors = []
        self.loaded = set()
        self.by_pointer = defaultdict(list)
        self.errors = []
        self._face_pointers = {}

    def page(self, page):
        self.errors = []
        for info in page.get_fonts(full=True):
            xref = info[0]
            if xref in self.loaded:
                continue
            self.loaded.add(xref)
            if not xref:
                self.errors.append("A direct font resource could not be bound to a PDF object.")
                continue
            try:
                pdf = fitz._as_pdf_document(self.document)
                obj = _M.pdf_new_indirect(pdf, xref, 0)
                desc = _M.ll_pdf_load_font(pdf.m_internal, _M.PdfObj().m_internal, obj.m_internal)
                self.descriptors.append(desc)
                resource = self.reader.get_object(xref)
                record = {"xref": xref, "mapping": None, "error": None, "standard": False,
                          "desc": desc, "embedded": bool(desc.is_embedded)}
                self.by_pointer[int(desc.font.this)].append(record)
                if "/ToUnicode" in resource:
                    try:
                        mapping, spaces = unicode_map(resource["/ToUnicode"], self.reader)
                        by_cid = defaultdict(set)
                        simple = resource.get("/Subtype") != "/Type0"
                        for code, value in mapping.items():
                            # Simple-font strings use one byte, but exporters
                            # may zero-pad ToUnicode keys to two bytes.
                            if simple and int.from_bytes(code, "big") > 255:
                                continue
                            if not simple and not any(len(lo) == len(code) and lo <= code <= hi for lo, hi in spaces):
                                raise ValueError("Mapping outside code space")
                            encoded = int.from_bytes(code, "big")
                            # MuPDF text items contain CIDs, not encoded codes.
                            # The actual encoding CMap binds ToUnicode entries.
                            cid = _M.ll_pdf_lookup_cmap(desc.encoding, encoded) if desc.encoding else encoded
                            if cid >= 0:
                                by_cid[cid].add(value)
                        record["mapping"] = dict(by_cid)
                    except Exception:
                        record["error"] = "The font has a malformed or unsupported ToUnicode map."
                else:
                    name = str(resource.get("/BaseFont", "")).lstrip("/")
                    encoding = resource.get("/Encoding")
                    record["standard"] = (name in STANDARD_FONTS and encoding is None) or (
                        isinstance(encoding, str) and encoding in STANDARD_ENCODINGS)
            except Exception:
                self.errors.append("A font resource could not be inspected.")

    def get(self, font):
        records = self.by_pointer.get(int(font.m_internal.this), [])
        raw = font.m_internal
        face = None
        if any(r["embedded"] for r in records) and raw.buffer:
            # Cache the byte hash once per retained native font pointer.
            key = (int(raw.this), raw.subfont)
            if key not in self._face_pointers:
                data = bytes(_M.ll_fz_buffer_storage_memoryview(raw.buffer, False))
                identity = (_digest(data), raw.subfont)
                if identity not in self.faces:
                    count = int.from_bytes(data[8:12], "big") if data[:4] == b"ttcf" else 1
                    if not 0 < count <= MAX_COLLECTION_FACES:
                        raise GlyphEvidenceError("The embedded font collection exceeds the face inspection limit.")
                    self.faces[identity] = _Face(data, raw.subfont)
                    if data[:4] == b"ttcf":
                        # TTFont validates each face. Other faces are independent
                        # evidence only; they never replace the native font ID.
                        for face_number in range(count):
                            other = (identity[0], face_number)
                            if other not in self.faces:
                                self.faces[other] = _Face(data, face_number)
                self._face_pointers[key] = self.faces[identity]
            face = self._face_pointers[key]
        return records, face

    def close(self):
        for desc in self.descriptors:
            _M.ll_pdf_drop_font(desc)
        self.descriptors.clear()

def _meaning(records, shape, cid, raw, has_ink, gid):
    issues, declared = [], []
    if not records:
        issues.append("The glyph could not be bound to a PDF font resource.")
    for record in records:
        if record["error"]:
            issues.append(record["error"])
        mapping = record["mapping"]
        if mapping is not None:
            values = mapping.get(cid, set())
            if len(values) != 1:
                issues.append("This used character code has conflicting Unicode mappings." if len(values) > 1 else
                              "This used character code has a missing or ambiguous Unicode mapping.")
                if len(records) > 1:
                    issues.append("A shared native font has an unverified PDF resource mapping.")
            else:
                declared.append(next(iter(values)))
        elif record["standard"]:
            declared.append(raw)
        elif len(records) > 1:
            issues.append("A shared native font has an unverified PDF resource mapping.")
    values = set(declared)
    if len(values) > 1:
        issues.append("Conflicting PDF resources share this native font identity.")
    value = next(iter(values)) if len(values) == 1 else None
    candidates = set(shape.get("candidates", ()))
    ligatures = set(shape.get("ligatures", ()))
    source = "pdf_mapping" if value is not None else "unresolved"
    if gid <= 0:
        issues.append("The text uses a missing or unbound glyph.")
    if value is not None and not _valid_unicode(value):
        issues.append("The declared text contains an invalid, private or replacement character.")
    if value and _invisible(value) and has_ink:
        issues.append("A drawing glyph is mapped to whitespace or an invisible character.")
    repeated = value and len(value) > 1 and len(set(value)) == 1 and value not in ligatures
    if repeated:
        issues.append("Repeated Unicode mapping lacks independent ligature evidence.")
    if value and not repeated and candidates and value not in candidates and value not in ligatures:
        # U+200B on a truly empty glyph is the sole normalization exception.
        if not (value == "\u200b" and not has_ink):
            issues.append("PDF Unicode and embedded glyph mappings disagree.")
    if value is None and len(candidates) == 1 and records and not any(r["error"] for r in records):
        value = next(iter(candidates))
        source = "embedded_cmap_ssty"
        issues = [issue for issue in issues if issue != "This used character code has a missing or ambiguous Unicode mapping."]
    elif repeated and len(candidates) == 1 and not any(r["error"] for r in records):
        # Recover the logical value from independent embedded evidence, but
        # retain the corrupt PDF declaration and its warning at this occurrence.
        value, source = next(iter(candidates)), "embedded_cmap_ssty"
    if value is None:
        value = raw or "\ufffd"
        issues.append("The glyph's logical Unicode identity is unresolved.")
    if not _valid_unicode(value):
        issues.append("The logical text contains an invalid, private or replacement character.")
    if has_ink and _invisible(value):
        issues.append("A drawing glyph is mapped to whitespace or an invisible character.")
    if not has_ink and not _invisible(value) and not all(unicodedata.combining(c) for c in value):
        issues.append("An extracted visible character has no glyph ink.")
    if shape.get("error"):
        issues.append(shape["error"])
    return value, sorted(set(issues)), source


class _Capture(_M.FzDevice2):
    """Capture every native text item, including unpainted and expanded items."""

    def __init__(self, resources):
        super().__init__()
        self.resources = resources
        self.glyphs = []
        self.previous = None
        self.page_number = 0
        self.event = 0
        self.paired = 0
        self.failure = None
        self.native_items = 0
        self.metatext = []
        self.actual_text = []
        for name in ("fill_text", "stroke_text", "clip_text", "clip_stroke_text", "ignore_text",
                     "begin_metatext", "end_metatext"):
            getattr(self, "use_virtual_" + name)()

    def begin_metatext(self, ctx, kind, value):
        if self.failure is None:
            self.metatext.append((kind, value, len(self.glyphs)))

    def end_metatext(self, ctx):
        if self.failure is not None:
            return
        if not self.metatext:
            self.failure = GlyphEvidenceError("PDF marked text metadata is unbalanced.")
            return
        kind, value, start = self.metatext.pop()
        if kind == _M.FZ_METATEXT_ACTUALTEXT:
            self.actual_text.append((self.page_number, value, start, len(self.glyphs)))

    def release(self):
        if self.previous:
            _M.ll_fz_drop_text(self.previous["held"])
            self.previous = None

    def _paint(self, mode, text, ctm, alpha=1., paint=None):
        if self.failure is not None:
            return
        try:
            self._collect(mode, text, ctm, alpha, paint)
        except Exception as exc:
            # SWIG callbacks can otherwise print and swallow Python failures.
            self.failure = exc

    def _collect(self, mode, text, ctm, alpha, paint):
        current = text.head
        count = 0
        while current:
            count += current.len
            if count + self.native_items > MAX_NATIVE_ITEMS:
                raise GlyphEvidenceError("The PDF exceeds the native glyph inspection limit.")
            current = current.next
        self.native_items += count
        matrix = tuple(getattr(ctm, name) for name in "abcdef")
        pointer = int(text.this)
        signature = []
        current = text.head
        while current:
            span = _M.FzTextSpan(current)
            signature.append((int(span.font().m_internal.this), tuple(getattr(span.trm(), c) for c in "abcdef"),
                              current.wmode, current.bidi_level,
                              tuple((span.items(i).gid, span.items(i).cid, span.items(i).ucs,
                                     span.items(i).x, span.items(i).y, span.items(i).adv) for i in range(current.len))))
            current = current.next
        previous = self.previous
        if (mode == "stroke" and previous and previous["mode"] == "fill"
                and previous["pointer"] == pointer and previous["matrix"] == matrix
                and previous["signature"] == signature
                and previous["paint"] == paint
                and previous["page"] == self.page_number and alpha == previous["alpha"] == 1.):
            # Retention prevents allocator pointer reuse from merging separate
            # overprints. This is one native object with two painting modes.
            for glyph in self.glyphs[previous["start"]:]:
                glyph["paint_modes"].append("stroke")
            self.paired += len(self.glyphs) - previous["start"]
            self.release()
            return
        self.release()
        start = len(self.glyphs)
        self.event += 1
        rawspan = text.head
        while rawspan:
            span = _M.FzTextSpan(rawspan)
            font = span.font()
            records, face = self.resources.get(font)
            last = None
            for index in range(rawspan.len):
                item = span.items(index)
                raw = chr(item.ucs) if 0 <= item.ucs <= 0x10ffff else "\ufffd"
                if item.gid < 0 and item.cid < 0 and last is not None and last["native_origin"] == [item.x, item.y]:
                    last["raw"] += raw
                    last["native_items"] += 1
                    continue
                trm = span.trm()
                trm.e, trm.f = item.x, item.y
                transform = _M.fz_concat(trm, _M.FzMatrix(ctm))
                origin = _M.fz_transform_point(_M.FzPoint(item.x, item.y), _M.FzMatrix(ctm))
                bounds = font.fz_bound_glyph(item.gid, transform) if item.gid >= 0 else _M.FzRect(origin.x, origin.y, origin.x, origin.y)
                bbox = [bounds.x0, bounds.y0, bounds.x1, bounds.y1]
                shape = face.glyph(item.gid) if face else {}
                tolerance = max(1., *(abs(v) for v in bbox)) * 2 ** -22
                ink = shape.get("empty") is False or (shape.get("empty") is None and
                    bbox[2] - bbox[0] > tolerance and bbox[3] - bbox[1] > tolerance)
                linear = [getattr(transform, name) for name in "abcd"]
                size = math.hypot(*linear[:2])
                direction = [linear[0] / size, linear[1] / size] if size else [0., 0.]
                last = {"page": self.page_number, "raw": raw, "gid": item.gid, "cid": item.cid,
                        "font": font.fz_font_name(), "font_key": face.key if face else None,
                        "resource_xrefs": sorted(r["xref"] for r in records),
                        "pdf_box": bbox, "origin": [origin.x, origin.y], "native_origin": [item.x, item.y],
                        "size": size, "direction": direction, "transform": linear,
                        "wmode": rawspan.wmode, "bidi_level": rawspan.bidi_level,
                        "advance": item.adv * size, "paint_modes": [mode], "opacity": alpha,
                        "native_items": 1, "event": self.event, "has_ink": ink,
                        "outline_key": shape.get("outline_key"), "script_role": None,
                        "_records": records, "_shape": shape}
                self.glyphs.append(last)
            rawspan = rawspan.next
        self.previous = {"held": _M.ll_fz_keep_text(text), "pointer": pointer, "matrix": matrix,
                         "page": self.page_number, "alpha": alpha, "mode": mode, "start": start,
                         "signature": signature, "paint": paint}

    def _color(self, cs, color):
        return (int(cs.this), tuple(_M.floats_getitem(color, i) for i in range(_M.ll_fz_colorspace_n(cs))))

    def fill_text(self, ctx, text, ctm, cs, color, alpha, params):
        self._paint("fill", text, ctm, alpha, self._color(cs, color))

    def stroke_text(self, ctx, text, stroke, ctm, cs, color, alpha, params):
        self._paint("stroke", text, ctm, alpha, self._color(cs, color))

    def clip_text(self, ctx, text, ctm, scissor):
        self._paint("clip", text, ctm)

    def clip_stroke_text(self, ctx, text, stroke, ctm, scissor):
        self._paint("clip_stroke", text, ctm)

    def ignore_text(self, ctx, text, ctm):
        self._paint("invisible", text, ctm, 0.)


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _box(page, bounds):
    rect = fitz.Rect(bounds) * page.rotation_matrix
    if not all(math.isfinite(v) for v in rect):
        raise GlyphEvidenceError("The PDF has invalid glyph coordinates.")
    return [max(0., min(1., value / size)) for value, size in
            zip(rect, (page.rect.width, page.rect.height) * 2)]


def _open(data):
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise GlyphEvidenceError("The PDF could not be opened.") from exc
    if document.needs_pass:
        document.close()
        raise GlyphEvidenceError("Password-protected PDFs cannot be compared.")
    if not len(document):
        document.close()
        raise GlyphEvidenceError("The PDF has no pages.")
    return document


def _project(a, b, direction):
    dx, dy = b[0] - a[0], b[1] - a[1]
    return dx * direction[0] + dy * direction[1], -dx * direction[1] + dy * direction[0]


def _layout(glyphs, page):
    """Use native text order; geometry supplies boundaries, never Unicode."""
    lines, line = [], []
    for glyph in glyphs:
        if line:
            previous = line[-1]
            along, across = _project(previous["origin"], glyph["origin"], previous["direction"])
            size = max(previous["size"], glyph["size"], 1.)
            known_line_change = (glyph.get("_layout_line") is not None and previous.get("_layout_line") is not None
                                 and glyph["_layout_line"] != previous["_layout_line"])
            if (known_line_change or glyph["direction"] != previous["direction"] or glyph["wmode"] != previous["wmode"]
                    or abs(across) > .8 * size or along < -.5 * size or along > 8 * size
                    or (glyph["event"] != previous["event"] and glyph["origin"] == previous["origin"])):
                lines.append(line)
                line = []
        line.append(glyph)
    if line:
        lines.append(line)
    for line_no, line in enumerate(lines):
        runs = []
        positions = {id(glyph): index for index, glyph in enumerate(line)}
        for glyph in line:
            glyph["line"] = line_no
            glyph["box"] = _box(page, glyph["pdf_box"])
            if glyph["wmode"] or not glyph["size"]:
                glyph["issues"].append("Vertical or degenerate text structure requires review.")
                continue
            if not glyph["has_ink"]:
                continue
            if runs:
                first, previous = runs[-1][0], runs[-1][-1]
                along, across = _project(previous["origin"], glyph["origin"], glyph["direction"])
                baseline = _project(first["origin"], glyph["origin"], glyph["direction"])[1]
                if (.95 <= glyph["size"] / first["size"] <= 1.05 and
                        abs(baseline) <= .02 * min(first["size"], glyph["size"]) and
                        -.4 * previous["size"] <= along - previous["advance"] <= .8 * previous["size"] and
                        not glyph.get("_space_before")):
                    runs[-1].append(glyph)
                    continue
            runs.append([glyph])
        # A script word attaches as a run. Its later letters must not become
        # baseline merely because they are farther from the base glyph.
        for index, run in enumerate(runs):
            first = run[0]
            candidates = []
            larger_run = False
            for other_run in runs[max(0, index - 8):index + 9]:
                if other_run is run or min(g["size"] for g in other_run) < max(g["size"] for g in run) * 1.18:
                    continue
                larger_run = True
                for other in other_run:
                    along, across = _project(other["origin"], first["origin"], first["direction"])
                    gap = along - other["advance"]
                    if -.4 * other["size"] <= gap <= .8 * other["size"] and abs(across) <= .8 * other["size"]:
                        offsets = [_project(other["origin"], g["origin"], g["direction"])[1] / other["size"] for g in run]
                        candidates.append((abs(gap), offsets))
            roles = set()
            for _, offsets in candidates:
                roles.update("sub" if dy > .12 else "super" if dy < -.12 else "baseline" for dy in offsets)
            if candidates:
                if len(roles) == 1:
                    offsets = min(candidates, key=lambda candidate: candidate[0])[1]
                    for glyph, offset in zip(run, offsets):
                        glyph["script_role"] = next(iter(roles))
                        glyph["baseline_offset"] = offset
                else:
                    for glyph in run:
                        glyph["issues"].append("Ambiguous subscript or superscript attachment.")
            elif larger_run:
                for glyph in run:
                    glyph["issues"].append("Mathematical baseline attachment is unresolved.")
            else:
                for glyph in run:
                    position = positions[id(glyph)]
                    peers = [other for other in line[max(0, position - 2):position + 3] if other is not glyph and other["has_ink"] and
                             .95 <= other["size"] / glyph["size"] <= 1.05 and
                             abs(_project(glyph["origin"], other["origin"], glyph["direction"])[1]) <= .02 * glyph["size"]]
                    if peers:
                        glyph["script_role"] = "baseline"
        for glyph in line:
            if glyph["script_role"] is None and "Mathematical baseline attachment is unresolved." not in glyph["issues"] and any(unicodedata.category(c) == "Sm" or
                    0x1D400 <= ord(c) <= 0x1D7FF for c in glyph["text"]):
                glyph["issues"].append("Mathematical baseline attachment is unresolved.")
    return lines


def _text_items(result, lines):
    for line in lines:
        text, expanded = "", []
        raw = "".join(g["raw"] for g in line)
        previous = None
        for glyph in line:
            value = "\ufffc" if glyph.get("opaque_key") else glyph["text"]
            if glyph.get("opaque_key"):
                glyph["text"] = value
            if value == "\u200b" and not glyph["has_ink"] and not glyph["issues"]:
                glyph["disposition"] = "normalized_control"
                result["controls"].append({"text": "U+200B", "page": glyph["page"], "box": glyph["box"],
                                           "context": raw, "glyph_id": glyph["id"]})
                continue
            if glyph["issues"] and (not value or value.isspace()):
                value = "\ufffd"
            if previous:
                along, across = _project(previous["origin"], glyph["origin"], previous["direction"])
                if not text[-1:].isspace() and not value.isspace() and glyph.get("_space_before"):
                    text += " "
                    expanded.append(None)
            glyph["disposition"] = "whitespace" if value.isspace() else "token"
            text += value
            expanded.extend([glyph] * len(value))
            previous = glyph
        for match in re.finditer(r"\S+", text):
            members = expanded[match.start():match.end()]
            item = {"text": match.group(), "page": line[0]["page"], "box": _union([g["box"] for g in members]),
                    "pdf_box": _union([g["pdf_box"] for g in members]), "occurrence": len(result["words"]),
                    "raw_context": raw, "issues": sorted({issue for g in members for issue in g["issues"]}),
                    "fonts": sorted({g["font"] for g in members}), "context_key": None, "semantic_context": None,
                    "_cell_order": (*reversed(line[0]["origin"]),
                                    _project(line[0]["origin"], members[0]["origin"], line[0]["direction"])[0]),
                    "text_identity_supported": all(g["text_identity_supported"] for g in members)}
            result["words"].append(item)
            for token in _TOKENS.finditer(match.group()):
                selected = members[token.start():token.end()]
                result["tokens"].append({**item, "text": token.group(), "word": match.group(),
                    "word_box": item["box"], "box": _union([g["box"] for g in selected]),
                    "glyphs": [g["box"] for g in selected],
                    "issues": sorted({issue for g in selected for issue in g["issues"]}),
                    "text_identity_supported": all(g["text_identity_supported"] for g in selected),
                    **({"opaque_key": selected[0]["opaque_key"], "raw": selected[0]["raw"]}
                       if len(selected) == 1 and selected[0].get("opaque_key") else {}),
                    "char_meta": [{"glyph_id": g["id"], "script_role": g["script_role"],
                                   "direction": g["direction"], "size": g["size"],
                                   "outline_key": g.get("pair_outline_key"),
                                   "baseline_offset": g.get("baseline_offset")} for g in selected],
                    "word_start": token.start(), "word_end": token.end()})


def _table_context(page, result, page_words, tables):
    ranks = {}
    tokens_by_word = defaultdict(list)
    for token in result["tokens"]:
        tokens_by_word[token["occurrence"]].append(token)
    def uncertain(word, reason):
        word["issues"] = sorted(set([*word["issues"], reason]))
        word["text_identity_supported"] = False
        for token in tokens_by_word[word["occurrence"]]:
            token["issues"] = sorted(set([*token["issues"], reason]))
            token["text_identity_supported"] = False
    for table_index, table in enumerate(tables):
        grid = table["grid"]
        members = defaultdict(list)
        ambiguous_cells = False
        for word in page_words:
            x0, y0, x1, y1 = word["pdf_box"]
            hits = [(r, c) for r, row in enumerate(grid) for c, cell in enumerate(row) if cell and
                    cell[0] <= (x0 + x1) / 2 <= cell[2] and cell[1] <= (y0 + y1) / 2 <= cell[3]]
            if len(hits) == 1:
                members[hits[0]].append(word)
            elif fitz.Rect(table["bbox"]).contains(fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)):
                ambiguous_cells = True
                uncertain(word, "The text cannot be assigned to an unambiguous table cell.")
        def read_label(row, column):
            # Ink tops differ between fonts even on one baseline. Native line
            # origins and progress along that line preserve its reading order.
            words = sorted(members.get((row, column), []), key=lambda w: w["_cell_order"])
            return " ".join(w["text"] for w in words) if words and all(
                w["text_identity_supported"] and "\ufffc" not in w["text"] for w in words) else None
        labels = {(r, c): read_label(r, c) for r, row in enumerate(grid) for c in range(len(row))}
        def label(row, column):
            return labels.get((row, column))
        headers = [label(0, c) for c in range(len(grid[0]))] if grid else []
        row_labels = [label(r, 0) for r in range(1, len(grid))]
        if headers and headers[0] is None and grid[0][0] and not members.get((0, 0)) and all(row_labels):
            corner = fitz.Rect(grid[0][0])
            occupied = any(g["page"] == page.number + 1 and (g["has_ink"] or g["raw"].strip() or g["issues"]) and
                           not (corner & fitz.Rect(g["pdf_box"])).is_empty for g in result["glyphs"])
            occupied = occupied or any(e["page"] == page.number + 1 and e.get("kind") in {"image", "vector"} and
                e.get("status") != "table_grid" and not (corner & fitz.Rect(e["pdf_box"])).is_empty
                for e in result["inventory"])
            if not occupied:
                # Typed structural identity cannot collide with literal text.
                headers[0] = ("empty_corner",)
        complete = (bool(headers) and all(headers) and len(set(headers)) == len(headers) and
                    all(row_labels) and not ambiguous_cells and
                    all(len(row) == len(headers) and all(row) for row in grid))
        descriptor = {"headers": headers, "row_labels": row_labels,
                      "members": members, "complete": complete, "id": (page.number, table_index)}
        result.setdefault("_tables", []).append(descriptor)
        first = min((w["occurrence"] for words in members.values() for w in words), default=0)
        for (r, c), words in sorted(members.items()):
            row_label = label(r, 0)
            context = {"kind": "table_cell", "headers": headers, "row_label": row_label,
                       "column_label": headers[c], "header": r == 0, "row": r, "column": c,
                       "_table_id": (page.number, table_index)}
            # Row/column ordinals are metadata only. Identity uses text labels;
            # duplicates are explicitly ambiguous, not silently uniquified.
            unique_row = row_label and sum(label(rr, 0) == row_label for rr in range(len(grid))) == 1
            unique_col = headers[c] and headers.count(headers[c]) == 1
            context["ambiguous"] = not (unique_col and (r == 0 or unique_row) and all(headers))
            key = None if context["ambiguous"] else json.dumps(
                ["table_cell", headers, "header" if r == 0 else row_label, headers[c]], ensure_ascii=True)
            for order, word in enumerate(sorted(words, key=lambda w: w["_cell_order"])):
                word["_identity_before_table"] = word["text_identity_supported"]
                word["context_key"], word["semantic_context"] = key, context
                for token in tokens_by_word[word["occurrence"]]:
                    token["_identity_before_table"] = token["text_identity_supported"]
                if context["ambiguous"]:
                    uncertain(word, "Table header or row identity is ambiguous.")
                ranks[word["occurrence"]] = (first, r, c, order)
                for token in tokens_by_word[word["occurrence"]]:
                    token["context_key"], token["semantic_context"] = key, context
    key = lambda item: ranks.get(item["occurrence"], (item["occurrence"], -1, -1, -1))
    result["words"].sort(key=key)
    result["tokens"].sort(key=key)


def _pair_table_context(results):
    """Use row ordinals only within a uniquely corresponding ordered table."""
    grouped = []
    for result in results:
        groups = defaultdict(list)
        for table in result.get("_tables", []):
            if table["complete"]:
                key = json.dumps([table["headers"], table["row_labels"]], ensure_ascii=True)
                groups[key].append(table)
        grouped.append(groups)
    for key in grouped[0].keys() & grouped[1].keys():
        if len(grouped[0][key]) != 1 or len(grouped[1][key]) != 1:
            continue
        for result, groups in zip(results, grouped):
            table = groups[key][0]
            tokens = defaultdict(list)
            for token in result["tokens"]:
                tokens[token["occurrence"]].append(token)
            for (r, c), words in table["members"].items():
                if r == 0 or table["row_labels"].count(table["row_labels"][r - 1]) == 1:
                    continue
                context_key = json.dumps(["table_ordered_cell", table["headers"], table["row_labels"], r, c], ensure_ascii=True)
                for word in words:
                    word["semantic_context"].update(ambiguous=False, correspondence="ordered_table")
                    for item in [word, *tokens[word["occurrence"]]]:
                        item["context_key"] = context_key
                        item["issues"] = [issue for issue in item["issues"] if issue != "Table header or row identity is ambiguous."]
                        item["text_identity_supported"] = item["_identity_before_table"]


def _axis_rule(drawing):
    items = drawing["items"]
    if len(items) == 1 and items[0][0] == "re":
        return fitz.Rect(items[0][1])
    if len(items) == 1 and items[0][0] == "l":
        _, a, b = items[0]
        return fitz.Rect(drawing["rect"]) if a.x == b.x or a.y == b.y else None
    if not items or any(item[0] != "l" for item in items):
        return None
    segments = [(tuple(item[1]), tuple(item[2])) for item in items]
    if any(a[1] != b[0] for a, b in zip(segments, segments[1:])):
        return None
    if drawing.get("closePath") and segments[-1][1] != segments[0][0]:
        segments.append((segments[-1][1], segments[0][0]))
    if segments[-1][1] != segments[0][0]:
        return None
    rect = fitz.Rect(drawing["rect"])
    if rect.is_empty:
        return None
    sides = defaultdict(list)
    for a, b in segments:
        if a == b:
            continue
        if a[0] == b[0] and a[0] in (rect.x0, rect.x1):
            sides[(0, a[0])].append(tuple(sorted((a[1], b[1]))))
        elif a[1] == b[1] and a[1] in (rect.y0, rect.y1):
            sides[(1, a[1])].append(tuple(sorted((a[0], b[0]))))
        else:
            return None
    for axis, edge in ((0, rect.x0), (0, rect.x1), (1, rect.y0), (1, rect.y1)):
        cursor = rect[1 - axis]
        for lo, hi in sorted(sides[(axis, edge)]):
            if lo != cursor:
                return None
            cursor = hi
        if cursor != rect[3 - axis]:
            return None
    return rect


def _table_grid_ownership(paths, tables):
    """Classify connected ruling paths, not arbitrary thin inline rectangles."""
    rules = []
    rectangles = []
    work = 0
    for index, drawing in enumerate(paths):
        work += len(drawing["items"])
        if work > MAX_TABLE_OWNERSHIP_WORK:
            return {}, True
        rect = _axis_rule(drawing)
        if rect is None:
            continue
        half = (drawing.get("width") or 0) / 2
        paint = rect + (-half, -half, half, half) if drawing["type"] in {"s", "fs"} else rect
        if rect.width > 0 and rect.height > 0:
            rectangles.append((index, rect, paint))
        if rect.width == rect.height:
            continue
        vertical = rect.height > rect.width
        rules.append((index, rect, paint, vertical))
    owned = {}
    for index, rect, paint, vertical in rules:
        # The path must span a cell edge, meeting perpendicular ruling at
        # both ends. A bar inside a cell, or an isolated fraction rule, fails.
        a, b = (1, 0) if vertical else (0, 1)
        # MuPDF coordinates are float32. This covers arithmetic roundoff in
        # touching path segments, not a visual/outline equality tolerance.
        tolerance = max(1., *(abs(v) for v in paint)) * 2 ** -22
        for ti, table in enumerate(tables):
            bounds = table["bbox"]
            if not (bounds[0] <= (rect.x0 + rect.x1) / 2 <= bounds[2] and
                    bounds[1] <= (rect.y0 + rect.y1) / 2 <= bounds[3]):
                continue
            if len(table["grid"]) < 2 or max(map(len, table["grid"]), default=0) < 2:
                continue
            work += sum(map(len, table["grid"]))
            if work > MAX_TABLE_OWNERSHIP_WORK:
                return owned, True
            if any(cell and rect.x0 < (cell[0] + cell[2]) / 2 < rect.x1 and
                   rect.y0 < (cell[1] + cell[3]) / 2 < rect.y1 for row in table["grid"] for cell in row):
                continue
            for ri, row in enumerate(table["grid"]):
                for ci, cell in enumerate(row):
                    work += 1
                    if work > MAX_TABLE_OWNERSHIP_WORK:
                        return owned, True
                    thickness = paint[b + 2] - paint[b]
                    full_edge = (abs(rect[a] - bounds[a]) <= thickness and
                                 abs(rect[a + 2] - bounds[a + 2]) <= thickness)
                    if not cell or not (full_edge or cell[a] <= rect[a] < rect[a + 2] <= cell[a + 2]):
                        continue
                    edges = [edge for edge in (cell[b], cell[b + 2]) if paint[b] <= edge <= paint[b + 2]]
                    for edge in edges:
                        connected = []
                        limits = bounds if full_edge else cell
                        for end, boundary in ((rect[a], limits[a]), (rect[a + 2], limits[a + 2])):
                            work += len(rules)
                            if work > MAX_TABLE_OWNERSHIP_WORK:
                                return owned, True
                            joined = False
                            for _, _, other_paint, other_vertical in rules:
                                if other_vertical == vertical:
                                    continue
                                # Exported ruling segments can stop short of
                                # their junction. Limit the join envelope by
                                # BOTH actual rule thicknesses, not glyph size.
                                join = max(tolerance, min(paint[b + 2] - paint[b],
                                                          other_paint[a + 2] - other_paint[a]) / 2)
                                if (other_paint[a] - join <= end <= other_paint[a + 2] + join and
                                        other_paint[a] <= boundary <= other_paint[a + 2] and
                                        max(other_paint[b], paint[b]) <= min(other_paint[b + 2], paint[b + 2]) + join):
                                    joined = True
                                    break
                            connected.append(joined)
                        if all(connected):
                            owned.setdefault(index, []).append([ti, ri, ci])
    # Some exporters paint the intersection separately from its ruling arms.
    # A junction must fit their actual thicknesses and a detected cell corner;
    # a small square elsewhere is still ordinary, unclassified graphics.
    for index, rect, paint in rectangles:
        if index in owned:
            continue
        verticals, horizontals = [], []
        tolerance = max(1., *(abs(v) for v in paint)) * 2 ** -22
        for other_index, _, other_paint, vertical in rules:
            work += 1
            if work > MAX_TABLE_OWNERSHIP_WORK:
                return owned, True
            if other_index not in owned:
                continue
            if (max(paint.x0, other_paint.x0) > min(paint.x1, other_paint.x1) + tolerance or
                    max(paint.y0, other_paint.y0) > min(paint.y1, other_paint.y1) + tolerance):
                continue
            if vertical and rect.width <= other_paint.width + tolerance:
                verticals.extend(owned[other_index])
            elif not vertical and rect.height <= other_paint.height + tolerance:
                horizontals.extend(owned[other_index])
        for ti, ri, ci in verticals:
            if not any(h[0] == ti for h in horizontals):
                continue
            cell = tables[ti]["grid"][ri][ci]
            if any(paint.x0 <= x <= paint.x1 and paint.y0 <= y <= paint.y1
                   for x in (cell[0], cell[2]) for y in (cell[1], cell[3])):
                owned.setdefault(index, []).append([ti, ri, ci])
    return owned, False


class _Segments(BasePen):
    """Convert quadratic curves exactly, not using an approximation pen."""

    def __init__(self):
        super().__init__()
        self.segments = []
        self.point = self.start = None

    def _point(self, point):
        return (Fraction(point[0]), -Fraction(point[1]))

    def _moveTo(self, point):
        self.point = self.start = self._point(point)

    def _lineTo(self, point):
        target = self._point(point)
        self.segments.append(("l", (self.point, target)))
        self.point = target

    def _curveToOne(self, a, b, c):
        a, b, c = map(self._point, (a, b, c))
        self.segments.append(("c", (self.point, a, b, c)))
        self.point = c

    def _qCurveToOne(self, control, target):
        control, target = self._point(control), self._point(target)
        a = tuple(p + Fraction(2, 3) * (q - p) for p, q in zip(self.point, control))
        b = tuple(p + Fraction(2, 3) * (q - p) for p, q in zip(target, control))
        self.segments.append(("c", (self.point, a, b, target)))
        self.point = target

    def _closePath(self):
        if self.point != self.start:
            self.segments.append(("l", (self.point, self.start)))
        self.point = self.start

    def _endPath(self):
        raise ValueError("Open font contour")


def _segment_key(segments):
    if not segments:
        return None
    segments = [(op, tuple(tuple(Fraction(v) for v in p) for p in values)) for op, values in segments]
    points = [point for _, values in segments for point in values]
    low = tuple(min(p[i] for p in points) for i in (0, 1))
    high = tuple(max(p[i] for p in points) for i in (0, 1))
    scale = max(high[0] - low[0], high[1] - low[1])
    if not scale:
        return None
    # Exact directed-segment multiset tolerates contour starting-point/order
    # differences, but preserves winding, aspect ratio, curves and reflection.
    normalized = [(op, tuple(tuple(str((Fraction(v) - low[i]) / scale) for i, v in enumerate(p))
                              for p in values)) for op, values in segments]
    return _digest(repr(sorted(normalized)).encode("ascii"))


def _drawing_key(drawing):
    segments = []
    for item in drawing["items"]:
        if item[0] in {"l", "c"}:
            segments.append((item[0], tuple(tuple(Fraction(v) for v in point) for point in item[1:])))
        elif item[0] == "re":
            rect, orientation = item[1:]
            points = [rect.tl, rect.tr, rect.br, rect.bl] if orientation == 1 else [rect.bl, rect.br, rect.tr, rect.tl]
            segments.extend(("l", (tuple(a), tuple(b))) for a, b in zip(points, points[1:] + points[:1]))
        else:
            return None
    if drawing.get("closePath") and segments and segments[-1][1][-1] != segments[0][1][0]:
        segments.append(("l", (segments[-1][1][-1], segments[0][1][0])))
    return _segment_key(segments)


def _drawing_appearance_key(drawing, path_key):
    if not path_key or drawing["type"] != "f" or drawing.get("fill_opacity") != 1. or drawing.get("fill") is None:
        return None
    return _digest(json.dumps([path_key, drawing["type"], drawing["fill"], drawing["fill_opacity"],
                               drawing.get("even_odd"), drawing.get("layer")],
                              separators=(",", ":"), allow_nan=False).encode("ascii"))


def _anchor_geometry(box, left, right):
    if not left or not right or left["size"] <= 0 or right["size"] <= 0:
        return None
    return {side: [str((Fraction(value) - Fraction(anchor["origin"][i % 2])) / Fraction(anchor["size"]))
                   for i, value in enumerate(box)] for side, anchor in (("left", left), ("right", right))}


def _enrich_candidates(results, faces):
    needed = defaultdict(list)
    gids = set()
    for result in results:
        for glyph in result["glyphs"]:
            if glyph["outline_key"] and (glyph["issues"] or not glyph["_shape"].get("candidates")):
                needed[glyph["outline_key"]].append(glyph)
                gids.add(glyph["gid"])
    # Subsets often retain original glyph IDs but omit cmap/GSUB. IDs and
    # names are search hints only: the acceptance test is exact outline equality.
    candidates = defaultdict(set)
    visited = set()
    incomplete = False
    for face in faces.values():
        if face.problem:
            continue
        indices = set(gid for gid in gids if 0 < gid < len(face.order))
        indices.update(face.font.getGlyphID(name) for name in face.script_names)
        for gid in indices:
            name = face.order[gid]
            if not face.meanings.get(name):
                continue
            shape = face.glyph(gid)
            visited.add((face.key, gid))
            if len(visited) > MAX_CANDIDATE_OUTLINES:
                incomplete = True
                break
            if shape["outline_key"] in needed:
                candidates[shape["outline_key"]].update(shape["candidates"])
        if incomplete:
            break
    if not incomplete and needed:
        # Renumbered subsets defeat glyph-ID hints. Scan the remaining declared
        # Unicode glyphs once, with a cached glyphset and a hard work ceiling.
        # A first hint match is not uniqueness proof: an unused glyph can map
        # the identical outline to a different Unicode value.
        # Empty glyf slots in sparse embedded subsets need no pen traversal.
        for face in faces.values():
            if face.problem:
                continue
            for name in face.meanings:
                gid = face.font.getGlyphID(name)
                if (face.key, gid) in visited:
                    continue
                visited.add((face.key, gid))
                if len(visited) > MAX_CANDIDATE_OUTLINES:
                    incomplete = True
                    break
                if "glyf" in face.font and face.font["glyf"][name].numberOfContours == 0:
                    continue
                shape = face.glyph(gid)
                if shape["outline_key"] in needed:
                    candidates[shape["outline_key"]].update(shape["candidates"])
            if incomplete:
                break
    if incomplete:
        for glyphs in needed.values():
            for glyph in glyphs:
                glyph["issues"].append("Embedded Unicode outline candidate inspection reached its work limit.")
        return
    repairable = {"Repeated Unicode mapping lacks independent ligature evidence.",
                  "The declared text contains an invalid, private or replacement character.",
                  "The logical text contains an invalid, private or replacement character.",
                  "This used character code has a missing or ambiguous Unicode mapping.",
                  "The glyph's logical Unicode identity is unresolved."}
    for key, glyphs in needed.items():
        for glyph in glyphs:
            values = set(glyph["_shape"].get("candidates", ())) | candidates[key]
            glyph["_shape"]["candidates"] = sorted(values)
            if len(values) == 1 and glyph["issues"] and set(glyph["issues"]) <= repairable:
                value = next(iter(values))
                if _valid_unicode(value) and not _invisible(value):
                    glyph["mapping_issues"] = glyph["issues"]
                    glyph.update(text=value, issues=[], source="embedded_cmap_ssty")


def _pair_evidence(results):
    catalog = defaultdict(set)
    declared = defaultdict(set)
    seen = []
    prototypes = defaultdict(dict)
    topologies = set()
    candidate_shapes = []
    for result in results:
        used = set()
        for glyph in result["glyphs"]:
            key = glyph["outline_key"]
            if not key:
                continue
            used.add(key)
            catalog[key].update(glyph["_shape"].get("candidates", ()))
            if not glyph["issues"]:
                catalog[key].add(glyph["text"])
                declared[key].add(glyph["text"])
            recording = glyph["_shape"].get("recording")
            if recording and key not in prototypes:
                pen = _Segments()
                try:
                    for op, points in recording:
                        getattr(pen, op)(*points)
                    vector_key = _segment_key(pen.segments)
                    prototypes[key] = {"vector_key": vector_key}
                    topologies.add(tuple(sorted(Counter(op for op, _ in pen.segments).items())))
                    points = [point for _, values in pen.segments for point in values]
                    width = max(p[0] for p in points) - min(p[0] for p in points)
                    height = max(p[1] for p in points) - min(p[1] for p in points)
                    if width and height:
                        candidate_shapes.append({"key": key, "aspect": float(width / height),
                                                 "curves": sum(op == "c" for op, _ in pen.segments),
                                                 "segments": len(pen.segments)})
                except (ValueError, TypeError, NotImplementedError):
                    prototypes[key] = {"vector_key": None}
        seen.append(used)
    shared = seen[0] & seen[1]
    recoverable = {"This used character code has a missing or ambiguous Unicode mapping.",
                   "The glyph's logical Unicode identity is unresolved.",
                   "Repeated Unicode mapping lacks independent ligature evidence.",
                   "The declared text contains an invalid, private or replacement character.",
                   "The logical text contains an invalid, private or replacement character."}
    for result in results:
        for glyph in result["glyphs"]:
            key = glyph["outline_key"]
            if key in shared:
                glyph["pair_outline_key"] = key
                if len(catalog[key]) == 1 and declared[key] and glyph["issues"] and set(glyph["issues"]) <= recoverable:
                    value = next(iter(catalog[key]))
                    if _valid_unicode(value) and not _invisible(value):
                        glyph["mapping_issues"] = glyph["issues"]
                        glyph.update(text=value, issues=[], source="exact_outline")
                if (glyph["issues"] and set(glyph["issues"]) <= recoverable
                        and len(catalog[key]) <= 1 and glyph["has_ink"]
                        and not glyph.get("layout_issue")):
                    # Identity is opaque: no replacement Unicode is inferred,
                    # and neither mapping issues nor coverage warnings vanish.
                    glyph["opaque_key"] = key
    vector_catalog = defaultdict(list)
    for key, info in prototypes.items():
        if info["vector_key"] and len(catalog[key]) == 1:
            value = next(iter(catalog[key]))
            if _valid_unicode(value) and not _invisible(value):
                vector_catalog[info["vector_key"]].append((key, value))
    for shape in candidate_shapes:
        shape["values"] = sorted(catalog[shape["key"]])
    return {"exact": vector_catalog, "topologies": topologies, "candidates": candidate_shapes}


def _vectors(result, catalog):
    by_page = defaultdict(list)
    for glyph in result["glyphs"]:
        if glyph["has_ink"]:
            by_page[glyph["page"]].append(glyph)
    for entry in result["inventory"]:
        drawing = entry.get("_drawing")
        if drawing is None or entry["status"] == "table_grid":
            continue
        x0, y0, x1, y1 = entry["pdf_box"]
        # Lines and large figures remain inventoried graphics, not invented
        # punctuation. Inline glyph candidates must sit between text neighbors.
        if not (x1 > x0 and y1 > y0) or drawing["type"] not in {"f", "fs"}:
            continue
        neighbors = [g for g in by_page[entry["page"]] if g["direction"] == [1., 0.] and
                     abs(g["origin"][1] - y1) <= g["size"] and y1 - y0 <= 2 * g["size"]]
        left = [g for g in neighbors if 0 <= x0 - g["pdf_box"][2] <= g["size"]]
        right = [g for g in neighbors if 0 <= g["pdf_box"][0] - x1 <= g["size"]]
        if not left and not right:
            continue
        left = min(left, key=lambda g: x0 - g["pdf_box"][2]) if left else None
        right = min(right, key=lambda g: g["pdf_box"][0] - x1) if right else None
        neighbor = left or right
        if left and right and abs(left["origin"][1] - right["origin"][1]) > .1 * max(left["size"], right["size"]):
            continue
        matches = catalog["exact"].get(_drawing_key(drawing), [])
        values = {value for _, value in matches}
        reliable = bool(left and right) and len(values) == 1 and drawing["type"] == "f" and drawing.get("fill_opacity") == 1.
        value = next(iter(values)) if reliable else "\ufffd"
        issues = [] if reliable else ["An inline vector glyph has no unique exact font-outline interpretation."]
        if not reliable:
            topology = Counter(item[0] for item in drawing["items"])
            curves = topology["c"]
            aspect = (x1 - x0) / (y1 - y0)
            # These loose checks nominate review regions ONLY. They never
            # supply Unicode, an equality key, or an accepted glyph recovery.
            candidates = [shape for shape in catalog["candidates"] if
                .5 <= aspect / shape["aspect"] <= 2. and
                ((curves and shape["curves"]) or
                 (not curves and not shape["curves"] and sum(topology.values()) >= 5) or
                 (aspect < .25 and any(v in {"|", "\u2223", "\u2016"} for v in shape["values"])))]
            if candidates and .3 * neighbor["size"] <= y1 - y0 <= 1.8 * neighbor["size"]:
                path_key = _drawing_key(drawing)
                entry.update(status="candidate_glyph", candidate_glyph=True, issues=issues,
                             path_key=path_key, appearance_key=_drawing_appearance_key(drawing, path_key),
                             anchor_geometry=_anchor_geometry(entry["pdf_box"], left, right),
                             candidate_values=sorted({v for shape in candidates for v in shape["values"]}),
                             _left_id=left["id"] if left else None, _right_id=right["id"] if right else None)
            continue
        glyph = {"id": len(result["glyphs"]), "page": entry["page"], "raw": "", "text": value,
            "gid": None, "cid": None, "font": "vector", "font_key": None, "resource_xrefs": [],
            "pdf_box": entry["pdf_box"], "origin": [x0, left["origin"][1]], "size": left["size"],
            "direction": [1., 0.], "transform": [left["size"], 0., 0., -left["size"]],
            "wmode": 0, "bidi_level": 0, "advance": x1 - x0, "paint_modes": ["vector"],
            "opacity": drawing.get("fill_opacity"), "native_items": 0, "event": left["event"],
            "has_ink": True, "outline_key": matches[0][0] if reliable else None, "script_role": None,
            "issues": issues, "source": "vector_outline" if reliable else "unresolved_vector"}
        result["glyphs"].append(glyph)
        entry.update(status="recovered" if reliable else "candidate_glyph", candidate_glyph=True,
                     glyph_id=glyph["id"], issues=issues)
        entry["_after"] = left["id"]
    # Only inserted vector occurrences are ordered spatially. Native text and
    # separate overprints keep their original occurrence order.
    insertions = {e["glyph_id"]: e["_after"] for e in result["inventory"] if "_after" in e}
    rank = {g["id"]: i for i, g in enumerate(result["glyphs"]) if g["id"] not in insertions}
    result["glyphs"].sort(key=lambda g: (rank.get(insertions.get(g["id"], g["id"]), len(rank)),
                                        g["id"] in insertions, g["origin"][0]))


def _collect_document(document, data, faces):
    result = {key: [] for key in ("words", "tokens", "pages", "controls", "glyphs", "inventory", "uncertain_regions")}
    resources = _Resources(document, data, faces)
    render_source = _RenderSource(resources.reader)
    capture = _Capture(resources)
    try:
        for number, page in enumerate(document, 1):
            rotation = page.rotation
            info = {"number": number, "width": page.rect.width, "height": page.rect.height, "rotation": rotation}
            try:
                info["render_source_key"] = render_source.page_key(number - 1)
            except Exception:
                # Optional equality proof cannot turn unsupported source graphs
                # into either successful equivalence or a parser-wide failure.
                info["render_source_key"] = None
                result["inventory"].append({"kind": "coverage", "page": number,
                    "issues": ["Exact page render-source identity could not be established."]})
            result["pages"].append(info)
            page.set_rotation(0)
            try:
                resources.page(page)
                capture.page_number = number
                start = len(capture.glyphs)
                page.this.fz_run_page_contents(capture, _M.FzMatrix(), _M.FzCookie())
                capture.release()
                if capture.failure:
                    if isinstance(capture.failure, GlyphEvidenceError):
                        raise capture.failure
                    raise GlyphEvidenceError("PDF glyph capture could not be completed.") from capture.failure
                glyphs = capture.glyphs[start:]
                for glyph in glyphs:
                    value, issues, source = _meaning(glyph.pop("_records"), glyph["_shape"], glyph["cid"],
                                                      glyph["raw"], glyph["has_ink"], glyph["gid"])
                    glyph.update(text=value, issues=issues, source=source, id=len(result["glyphs"]))
                    if glyph["paint_modes"] not in (["fill"], ["stroke"], ["fill", "stroke"]) or glyph["opacity"] != 1.:
                        glyph["issues"].append("Invisible, clipped or translucent text requires visual review.")
                    glyph.pop("native_origin")
                    result["glyphs"].append(glyph)
                _native_boundaries(page, glyphs)
                paths = page.get_drawings()
                finder = page.find_tables(paths=paths) if paths else None
                if paths and finder is None:
                    raise GlyphEvidenceError("PDF table geometry could not be inspected.")
                tables = [{"bbox": list(table.bbox),
                           "grid": [[list(cell) if cell else None for cell in row.cells] for row in table.rows]}
                          for table in finder.tables] if finder else []
                owned, ownership_limited = _table_grid_ownership(paths, tables)
                if ownership_limited:
                    result["inventory"].append({"kind": "coverage", "page": number,
                        "issues": ["Table ruling ownership inspection reached its work limit."]})
                # Retain drawing occurrence coverage even when no font-backed
                # interpretation is available. It is never a clean omission.
                for index, drawing in enumerate(paths):
                    result["inventory"].append({"kind": "vector", "page": number,
                        "pdf_box": list(drawing["rect"]), "status": "table_grid" if index in owned else "unclassified",
                        "issues": ["Table ruling appearance is outside logical glyph equality." if index in owned else
                                   "Vector drawing is outside established logical glyph coverage."],
                        **({"table_cells": owned[index]} if index in owned else {}), "_drawing": drawing})
                for image in page.get_image_info():
                    result["inventory"].append({"kind": "image", "page": number, "pdf_box": list(image["bbox"]),
                        "status": "unclassified", "issues": ["Image content is outside logical glyph coverage."]})
                for error in resources.errors:
                    result["inventory"].append({"kind": "resource", "page": number, "issues": [error]})
                info["_tables"] = tables
            finally:
                page.set_rotation(rotation)
        for number, value, start, end in capture.actual_text:
            members = result["glyphs"][start:end]
            result["inventory"].append({"kind": "actual_text", "page": number,
                "status": "metadata", "text": value, "glyph_ids": [g["id"] for g in members],
                "raw": "".join(g["raw"] for g in members), "issues": [],
                **({"pdf_box": _union([g["pdf_box"] for g in members])} if members else {})})
        result["_paired"] = capture.paired
        return result
    finally:
        capture.release()
        capture.fz_close_device()
        resources.close()


def _native_boundaries(page, glyphs):
    flags = ((fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES & ~fitz.TEXT_USE_CID_FOR_UNKNOWN_UNICODE)
             | fitz.TEXT_IGNORE_ACTUALTEXT)
    boundaries = defaultdict(list)
    for bi, block in enumerate(page.get_text("rawdict", flags=flags, sort=False)["blocks"]):
        for li, line in enumerate(block.get("lines", [])):
            space = False
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"].isspace():
                        space = True
                    else:
                        boundaries[(tuple(char["origin"]), char["c"])].append(((bi, li), space))
                        space = False
    for glyph in glyphs:
        matches = boundaries.get((tuple(glyph["origin"]), glyph["raw"][:1]), [])
        bindings = set(matches)
        if len(bindings) == 1:
            glyph["_layout_line"], glyph["_space_before"] = next(iter(bindings))
        elif glyph["raw"].strip():
            glyph["layout_issue"] = "Native word/line boundary binding is ambiguous or unavailable."


def _prepare_layout(document, result):
    for number, page in enumerate(document, 1):
        glyphs = [g for g in result["glyphs"] if g["page"] == number]
        for glyph in glyphs:
            glyph["text_identity_supported"] = not glyph["issues"] or bool(glyph.get("opaque_key"))
            if glyph.get("layout_issue"):
                glyph["issues"].append(glyph["layout_issue"])
                glyph["text_identity_supported"] = False
                glyph.pop("opaque_key", None)
        lines = _layout(glyphs, page)
        start = len(result["words"])
        _text_items(result, lines)
        rotation = page.rotation
        page.set_rotation(0)
        try:
            _table_context(page, result, result["words"][start:], result["pages"][number - 1].pop("_tables"))
        finally:
            page.set_rotation(rotation)
        for entry in result["inventory"]:
            if entry["page"] == number and "pdf_box" in entry:
                entry["box"] = _box(page, entry["pdf_box"])


def _finish(result):
    identities = defaultdict(set)
    for word in result["words"]:
        if word["context_key"]:
            identities[word["context_key"]].add(word["semantic_context"]["_table_id"])
    for item in [*result["words"], *result["tokens"]]:
        if item["context_key"] and len(identities[item["context_key"]]) > 1:
            item["issues"] = sorted(set([*item["issues"], "Separate tables have indistinguishable semantic cell identities."]))
            item["semantic_context"]["ambiguous"] = True
            item["context_key"] = None
            item["text_identity_supported"] = False
    for word in result["words"]:
        if word["semantic_context"]:
            word["semantic_context"].pop("_table_id", None)
    for item in [*result["words"], *result["tokens"]]:
        item.pop("_cell_order", None)
        item.pop("_identity_before_table", None)
    result.pop("_tables", None)
    for glyph in result["glyphs"]:
        glyph.pop("_shape", None)
        glyph.pop("_layout_line", None)
        glyph.pop("_space_before", None)
    for entry in result["inventory"]:
        entry.pop("_drawing", None)
        entry.pop("_after", None)
    counts = Counter(g["disposition"] for g in result["glyphs"])
    unresolved = sum(bool(g["issues"]) for g in result["glyphs"])
    result["coverage_reasons"] = sorted({issue for item in [*result["glyphs"], *result["inventory"], *result["words"]] for issue in item["issues"]})
    result["coverage"] = {"complete": not result["coverage_reasons"], "glyphs": len(result["glyphs"]),
        "native_text_items": sum(g["native_items"] for g in result["glyphs"]),
        "token_glyphs": counts["token"], "whitespace_glyphs": counts["whitespace"],
        "normalized_controls": counts["normalized_control"], "unresolved_glyphs": unresolved,
        "tokens": len(result["tokens"]), "inventory_items": len(result["inventory"]),
        "accounted_glyphs": sum(counts.values())}
    token_for_glyph = {}
    for index, token in enumerate(result["tokens"]):
        for meta in token["char_meta"]:
            token_for_glyph.setdefault(meta["glyph_id"], index)
    for entry in result["inventory"]:
        if entry.get("candidate_glyph") and entry["status"] != "recovered":
            region = {"page": entry["page"], "box": entry["box"], "issues": entry["issues"],
                      "kind": "vector_glyph", "candidate_values": entry.get("candidate_values", []),
                      "path_key": entry.get("path_key"), "appearance_key": entry.get("appearance_key"),
                      "anchor_geometry": entry.get("anchor_geometry")}
            for side in ("left", "right"):
                index = token_for_glyph.get(entry.get("_" + side + "_id"))
                region[side + "_token_index"] = index
                region[side + "_token_key"] = None if index is None else {
                    "text": result["tokens"][index]["text"], "context_key": result["tokens"][index]["context_key"]}
            result["uncertain_regions"].append(region)
        entry.pop("_left_id", None)
        entry.pop("_right_id", None)
    result["evidence_summary"] = {"sources": dict(Counter(g["source"] for g in result["glyphs"])),
        "unresolved_glyphs": unresolved, "fill_stroke_pairs": result.pop("_paired"),
        "exact_outline_glyphs": sum(bool(g.get("pair_outline_key")) for g in result["glyphs"]),
        "opaque_glyphs": sum(bool(g.get("opaque_key")) for g in result["glyphs"]),
        "actual_text_spans": sum(e["kind"] == "actual_text" for e in result["inventory"]),
        "table_grid_paths": sum(e.get("status") == "table_grid" for e in result["inventory"]),
        "vector_candidates": len(result["uncertain_regions"])}
    return result


def inspect_pdf_pair(before: bytes, after: bytes) -> tuple[dict, dict]:
    """Inspect supplied bytes only; identical inputs never waive uncertainty."""
    faces = {}
    try:
        with ExitStack() as stack:
            documents = [stack.enter_context(_open(data)) for data in (before, after)]
            results = [_collect_document(doc, data, faces) for doc, data in zip(documents, (before, after))]
            _enrich_candidates(results, faces)
            catalog = _pair_evidence(results)
            for result in results:
                _vectors(result, catalog)
            for document, result in zip(documents, results):
                _prepare_layout(document, result)
            _pair_table_context(results)
            return tuple(_finish(result) for result in results)
    finally:
        for face in faces.values():
            face.close()
