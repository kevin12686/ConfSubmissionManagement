"""Convert OCR evidence to the signed comparison workspace contract."""
from collections import Counter, defaultdict
import math
import re

from . import engine, hyphens, symbols

VERSION = 9
LIMITATIONS = [
    "OCR compares recognized visible text, not PDF metadata or native text. Identical OCR errors and missed text can hide real changes.",
    "Whitespace split/join and the listed direct visual substitutions are tolerated; genuine similar-character edits can be missed.",
    "Hyphen-only Latin word differences are information, not proof of equal meaning; ambiguous named-variable expressions still need visual review.",
    "Figures, image preservation, full mathematical structure and layout require manual review. This result is not publication approval.",
]
ERROR_PATTERN = re.compile(r"Error!\s*(?:Reference source not found|Bookmark not defined)\.?|\[\s*\?\s*\]|\?\?|(?:Figure|Fig\.|Table|Eq\.|Equation|Section)\s*\?", re.I)


def _patterns(pages):
    lines = [(p["page"], line) for p in pages for line in p["ocr"]["lines"]]
    text = ""
    ranges = []
    for page, line in lines:
        start = len(text)
        text += line["text"] + "\n"
        ranges.append((start, len(text), page, line))
    for match in ERROR_PATTERN.finditer(text):
        locations, raw = [], []
        for start, end, page, line in ranges:
            if start < match.end() and end > match.start():
                locations.append({"page": page, "box": line["box"], "precision": "line"})
                raw.append({"page": page, "box": line["box"], "text": line["text"], "score": line["score"]})
        yield " ".join(match.group().lower().split()), match.group(), locations, raw


def build_result(before_pages, after_pages, before_native=None, after_native=None, *,
                 identical_bytes=False, max_findings=300, max_boxes=160):
    evidence = engine.compare_pages(before_pages, after_pages, before_native, after_native)
    pages = {"before": before_pages, "after": after_pages}
    rules = {row["id"]: row for row in symbols.table()}
    findings = []
    totals = Counter()
    reasons = list(LIMITATIONS)
    info_kinds = Counter()
    votes = evidence["page_alignment"]

    def page_map(side, other):
        grouped = defaultdict(Counter)
        for row in votes:
            grouped[row[side]][row[other]] += row["supported_tokens"]
        result = {}
        for number, options in grouped.items():
            best = options.most_common()
            if len(best) == 1 or best[0][1] > best[1][1]:
                result[str(number)] = best[0][0]
        return result

    forward, inverse = page_map("before", "after"), page_map("after", "before")

    def box(side, loc):
        page = pages[side][loc["page"] - 1]
        return [max(0.0, min(1.0, float(value) / page["width" if i % 2 == 0 else "height"]))
                for i, value in enumerate(loc["box"])]

    def append(item, category, label):
        totals[category] += 1
        if category in {"formatting", "tolerated"}:
            info_kinds[item["kind"]] += 1
        if len(findings) >= max_findings:
            return
        finding = {"id": len(findings), "category": category, "kind": item["kind"],
                   "label": label, "issues": [item["reason"]], "fonts": [], "locations_truncated": False}
        for side in ("before", "after"):
            locations = item[side]
            actual = sorted({loc["page"] for loc in locations})
            finding[side + "_locations"] = [{"page": loc["page"], "box": box(side, loc),
                                            "precision": loc.get("precision", "line")} for loc in locations[:max_boxes]]
            finding[side + "_pages"] = actual
            finding[side + "_page"] = actual[0] if actual else None
            finding[side + "_text"] = item[side + "_text"][:2400]
            finding[side + "_context"] = item.get(side + "_context", "")[:2400]
            raw = item.get(side + "_ocr", [])
            finding[side + "_ocr"] = [dict(row, box=box(side, row)) for row in raw[:max_boxes]]
            finding[side + "_raw"] = "\n".join(row["text"] for row in raw)[:4800]
            if len(locations) > max_boxes or len(raw) > max_boxes or len(item[side + "_text"]) > 2400:
                finding["locations_truncated"] = True
        finding["before_reference_page"] = inverse.get(str(finding["after_page"])) if not finding["before_pages"] else None
        finding["after_reference_page"] = forward.get(str(finding["before_page"])) if not finding["after_pages"] else None
        if item.get("rule_id"):
            rule = rules[item["rule_id"]]
            finding.update(rule_id=rule["id"], rule_label=rule["label"], rule_risk=rule["risk"])
        findings.append(finding)

    for group, category, label in (("findings", "text", "OCR text difference"),
                                   ("handoffs", "uncertain", "Unable to locate corresponding text"),
                                   ("info", "formatting", "Possible line-wrap hyphen"),
                                   ("ignored_spacing", "formatting", "Tolerated whitespace split/join"),
                                   ("ignored_visual", "tolerated", "Tolerated visual substitution")):
        for item in evidence[group]:
            append(item, category, "Hyphen-only word difference" if item['kind'] == 'word_hyphen' else label)
    prior = Counter(key for key, _, _, _ in _patterns(before_pages))
    for key, text, locations, raw in _patterns(after_pages):
        if prior[key]:
            prior[key] -= 1
            continue
        append({"kind": "reference", "reason": "This error pattern occurs more often in the corrected PDF's OCR. Verify the printed page; recognition can be wrong.",
                "before_text": "", "after_text": text, "before": [], "after": locations, "after_ocr": raw},
               "reference", "Possible reference error")
    empty_pages = 0
    for side in ("before", "after"):
        for page in pages[side]:
            if page["ocr"]["lines"]:
                continue
            empty_pages += 1
            item = {"kind": "no_ocr_text", "reason": "No OCR text was detected on this page; blank versus unreadable is not certified.",
                    "before": [], "after": [], "before_text": "", "after_text": ""}
            item[side] = [{"page": page["page"], "box": [0, 0, page["width"], page["height"]], "precision": "page"}]
            append(item, "uncertain", "No OCR text detected")
    if evidence["handoffs"]:
        reasons.append("Some regions exceed local alignment limits or have no reliable counterpart. Inspect both PDFs.")
    if empty_pages:
        reasons.append("One or more pages have no OCR text. No text equality is claimed for those pages.")
    total = sum(totals.values())
    if total > len(findings) or any(f["locations_truncated"] for f in findings):
        reasons.append("The display limit was reached; not all findings, text or locations are shown.")
    counts = Counter(f["category"] for f in findings)
    available = all(any(page["ocr"]["lines"] for page in pages[side]) for side in pages)
    summary = {}
    for side in pages:
        summary[side] = {"ocr_pages": len(pages[side]),
                         "ocr_lines": sum(len(p["ocr"]["lines"]) for p in pages[side]),
                         "ocr_characters": evidence["counts"][side + "_characters"],
                         "unlocated_tokens": evidence["counts"]["unknown_" + side],
                         "row_reorders": evidence["locator"][side].get("row_reorders", 0)}
    return {
        "engine": "rapidocr", "visual_profile": "broad", "visual_exception_table": list(rules.values()),
        "comparison_status": evidence["status"], "findings": findings, "counts": dict(counts), "total_counts": dict(totals),
        "before_pages": [{"number": p["page"], "width": p.get("pdf_width", p["width"]), "height": p.get("pdf_height", p["height"])} for p in before_pages],
        "after_pages": [{"number": p["page"], "width": p.get("pdf_width", p["width"]), "height": p.get("pdf_height", p["height"])} for p in after_pages],
        "page_map": forward, "inverse_map": inverse, "text_available": available,
        "truncated": total > len(findings), "pairing_limited": bool(evidence["handoffs"]),
        "total_findings": total, "shown_findings": len(findings),
        "total_attention_count": total - totals["formatting"] - totals["tolerated"],
        "attention_count": sum(f["category"] not in {"formatting", "tolerated"} for f in findings),
        "uncertain_count": totals["uncertain"], "content_count": totals["text"] + totals["reference"],
        "info_count": totals["formatting"], "tolerated_count": totals["tolerated"],
        "shown_tolerated_count": counts["tolerated"], "info_kind_counts": dict(info_kinds),
        "identical_bytes": identical_bytes, "coverage_status": "limited", "coverage_reasons": reasons,
        "evidence_summary": summary, "ocr_accounting": dict(evidence["counts"], groups={
            group: {side: sum(item[side + "_characters"] for item in evidence[group]) for side in pages}
            for group in ("findings", "handoffs", "info", "ignored_spacing", "ignored_visual")}),
    }


def validate_result(result):
    """Fail closed on malformed, inconsistent or out-of-range worker output."""
    if not isinstance(result, dict) or result.get("engine") != "rapidocr" or result.get("visual_profile") != "broad":
        raise ValueError("Invalid OCR comparison response")
    if result.get("coverage_status") != "limited" or not isinstance(result.get("coverage_reasons"), list) or not result["coverage_reasons"]:
        raise ValueError("Missing OCR limitations")
    if any(not isinstance(reason, str) or not reason for reason in result["coverage_reasons"]):
        raise ValueError("Invalid OCR limitations")
    if result.get("comparison_status") not in {"compared", "partial", "unlocated", "no_ocr_text"}:
        raise ValueError("Invalid comparison status")
    for flag in ("truncated", "text_available", "pairing_limited", "identical_bytes"):
        if type(result.get(flag)) is not bool:
            raise ValueError("Invalid result flags")
    for side in ("before", "after"):
        pages = result.get(side + "_pages")
        if not isinstance(pages, list) or not 0 < len(pages) <= 40:
            raise ValueError("Invalid OCR page inventory")
        for number, page in enumerate(pages, 1):
            if not isinstance(page, dict) or page.get("number") != number or any(
                type(page.get(key)) not in (int, float) or not math.isfinite(page[key]) or not 0 < page[key] <= 10000 for key in ("width", "height")
            ):
                raise ValueError("Invalid OCR page dimensions")

    def location(row, side):
        if not isinstance(row, dict) or type(row.get("page")) is not int or not 1 <= row["page"] <= len(result[side + "_pages"]):
            raise ValueError("Invalid OCR location page")
        box = row.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1 for x in box) or box[0] > box[2] or box[1] > box[3]:
            raise ValueError("Invalid OCR location box")

    findings = result.get("findings")
    if not isinstance(findings, list) or len(findings) > 300:
        raise ValueError("Invalid finding inventory")
    categories = {"text": {"added", "changed", "removed"}, "reference": {"reference"}, "uncertain": {"author_review", "no_ocr_text"},
                  "formatting": {"wrap_hyphen", "word_hyphen", "ignored_spacing"}, "tolerated": {"ignored_visual"}}
    for number, row in enumerate(findings):
        if not isinstance(row, dict) or row.get("id") != number or row.get("kind") not in categories.get(row.get("category"), set()):
            raise ValueError("Invalid finding identity")
        if not isinstance(row.get("issues"), list) or not row["issues"] or any(not isinstance(x, str) for x in row["issues"]):
            raise ValueError("Invalid finding explanation")
        if row['kind'] in {'word_hyphen', 'wrap_hyphen'} and (
            not isinstance(row.get('before_text'), str) or not isinstance(row.get('after_text'), str)
            or not hyphens.text_equivalent(row['before_text'], row['after_text'])
        ):
            raise ValueError('Invalid hyphen-only word evidence')
        if type(row.get("locations_truncated")) is not bool:
            raise ValueError("Invalid location coverage")
        for side in ("before", "after"):
            for key in ("text", "raw", "context"):
                if not isinstance(row.get(side + "_" + key), str) or len(row[side + "_" + key]) > 4800:
                    raise ValueError("Invalid OCR finding text")
            actual = row.get(side + "_pages")
            if not isinstance(actual, list) or actual != sorted(set(actual)) or any(type(p) is not int or not 1 <= p <= len(result[side + "_pages"]) for p in actual):
                raise ValueError("Invalid finding pages")
            if row.get(side + "_page") != (actual[0] if actual else None):
                raise ValueError("Invalid finding first page")
            for key in ("locations", "ocr"):
                records = row.get(side + "_" + key)
                if not isinstance(records, list) or len(records) > 160:
                    raise ValueError("Invalid finding location inventory")
                if row['kind'] in {'word_hyphen', 'wrap_hyphen'} and not records:
                    raise ValueError('Missing hyphen-only word location evidence')
                for record in records:
                    location(record, side)
                    if record["page"] not in actual:
                        raise ValueError("Finding page not accounted for")
                    if key == "ocr" and (not isinstance(record.get("text"), str) or type(record.get("score")) not in (int, float) or not math.isfinite(record["score"])):
                        raise ValueError("Invalid OCR provenance")
            reference = row.get(side + "_reference_page")
            if reference is not None and (actual or type(reference) is not int or not 1 <= reference <= len(result[side + "_pages"])):
                raise ValueError("Invalid reference page")
    for key, side, other in (("page_map", "before", "after"), ("inverse_map", "after", "before")):
        mapping = result.get(key)
        if not isinstance(mapping, dict) or any(str(p) not in {str(n) for n in range(1, len(result[side + "_pages"]) + 1)} or type(q) is not int or not 1 <= q <= len(result[other + "_pages"]) for p, q in mapping.items()):
            raise ValueError("Invalid page correspondence")
    counts = Counter(f["category"] for f in findings)
    totals = result.get("total_counts")
    if not isinstance(totals, dict) or any(key not in categories or type(n) is not int or n < counts[key] for key, n in totals.items()) or any(totals.get(key, 0) < n for key, n in counts.items()):
        raise ValueError("Invalid total counts")
    if result.get("counts") != dict(counts):
        raise ValueError("Invalid displayed counts")
    total = sum(totals.values())
    expected = {"total_findings": total, "shown_findings": len(findings),
                "content_count": totals.get("text", 0) + totals.get("reference", 0), "uncertain_count": totals.get("uncertain", 0),
                "info_count": totals.get("formatting", 0), "tolerated_count": totals.get("tolerated", 0),
                "shown_tolerated_count": counts["tolerated"],
                "total_attention_count": totals.get("text", 0) + totals.get("reference", 0) + totals.get("uncertain", 0),
                "attention_count": counts["text"] + counts["reference"] + counts["uncertain"]}
    if any(type(result.get(key)) is not int or result[key] != value for key, value in expected.items()) or result["truncated"] != (total > len(findings)):
        raise ValueError("Inconsistent result accounting")
    if result.get("visual_exception_table") != symbols.table():
        raise ValueError("Unknown visual exception policy")
    rules = {rule["id"]: rule for rule in symbols.table()}
    for row in findings:
        if row["category"] == "tolerated":
            rule = rules.get(row.get("rule_id"))
            if not rule or row.get("rule_label") != rule["label"] or row.get("rule_risk") != rule["risk"]:
                raise ValueError("Invalid visual exception evidence")
    accounting, summary = result.get("ocr_accounting"), result.get("evidence_summary")
    groups = {"findings", "handoffs", "info", "ignored_spacing", "ignored_visual"}
    if not isinstance(accounting, dict) or not isinstance(accounting.get("groups"), dict) or set(accounting["groups"]) != groups or not isinstance(summary, dict):
        raise ValueError("Missing OCR occurrence accounting")
    for key, value in accounting.items():
        if key != "groups" and (type(value) is not int or not 0 <= value <= 600000):
            raise ValueError("Invalid OCR accounting count")
    for group in accounting["groups"].values():
        if not isinstance(group, dict) or set(group) != {"before", "after"} or any(type(n) is not int or not 0 <= n <= 600000 for n in group.values()):
            raise ValueError("Invalid OCR group accounting")
    equal = accounting.get("equal_characters")
    if type(equal) is not int:
        raise ValueError("Missing OCR equality accounting")
    for side in ("before", "after"):
        total_chars = accounting.get(side + "_characters")
        if type(total_chars) is not int or total_chars != equal + sum(group[side] for group in accounting["groups"].values()):
            raise ValueError("OCR characters omitted or counted twice")
        side_summary = summary.get(side)
        if not isinstance(side_summary, dict) or any(type(n) is not int or n < 0 for n in side_summary.values()) or side_summary.get("ocr_characters") != total_chars or side_summary.get("ocr_pages") != len(result[side + "_pages"]):
            raise ValueError("Inconsistent OCR evidence summary")
    return result
