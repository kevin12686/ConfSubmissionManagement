"""Read-only sample acceptance; cached regression is NOT fresh OCR acceptance.

Run with the application's Python and explicit local model configuration:
  CFM_RAPIDOCR_MODEL_DIR=/absolute/models python scripts/accept_rapidocr_real3pairs.py \
    --manifest /absolute/study/manifest.json --output /absolute/new-output

The supplied real pairs are unadjudicated. This checks execution, accounting,
frozen cached regression and source integrity, not semantic defect accuracy.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text())


def check_accounting(result):
    counts = result["counts"]
    if "before_characters" not in counts:
        require(result["status"] == "no_ocr_text" and result["warnings"], "Missing character accounting")
        require(not any(result[key] for key in ("findings", "handoffs", "info", "ignored_spacing", "ignored_visual")),
                "Unaccounted findings")
        return
    for side in ("before", "after"):
        accounted = counts["equal_characters"] + sum(
            item[f"{side}_characters"]
            for category in ("findings", "handoffs", "info", "ignored_spacing", "ignored_visual")
            for item in result[category]
        )
        require(accounted == counts[f"{side}_characters"], f"{side}: incomplete character accounting")
    for category, count in (("findings", "changes"), ("handoffs", "author_review"),
                            ("info", "info"), ("ignored_spacing", "ignored_spacing"),
                            ("ignored_visual", "ignored_visual")):
        require(len(result[category]) == counts[count], f"{category}: inconsistent count")


def cached_pages(document, fingerprints):
    pages = []
    for page in document["pages"]:
        image = Path(page["image"])
        require(sha256(image) == page["image_sha256"], f"Changed rendered image: {image}")
        source = Path(document["ocr_root"]) / f"{page['id']}.json"
        ocr = read_json(source)
        require(ocr["image_sha256"] == page["image_sha256"], f"Unbound cached OCR: {source}")
        require(ocr["status"] == "completed", f"Failed cached OCR: {source}")
        fingerprints[str(image)] = page["image_sha256"]
        fingerprints[str(source)] = sha256(source)
        pages.append(dict(page, ocr=ocr))
    return pages


def legacy_fields(value):
    if isinstance(value, dict):
        return {key: legacy_fields(item) for key, item in value.items() if key not in {"before_ocr", "after_ocr"}}
    if isinstance(value, list):
        return [legacy_fields(item) for item in value]
    return value


def migrate_blank_reference(result):
    """The port uses character accounting even when neither page has OCR text."""
    result = legacy_fields(result)
    if result["status"] == "no_ocr_text" and "equal_pairs" in result["counts"]:
        require(result.pop("anchors") == [], "Unexpected anchors without OCR text")
        for key in ("before_tokens", "after_tokens", "equal_pairs"):
            require(result["counts"].pop(key) == 0, "Nonempty legacy blank accounting")
        result["counts"].update(before_characters=0, after_characters=0, equal_characters=0)
    return result


def check_provenance(result, pages):
    for group in ("findings", "handoffs", "info", "ignored_spacing", "ignored_visual"):
        for finding in result[group]:
            for side in ("before", "after"):
                raw = finding[side + "_ocr"]
                require(bool(raw) == bool(finding[side]), f"{group}: missing or invented {side} OCR provenance")
                seen = set()
                for row in raw:
                    key = (row["page"], row["text"], row["score"], tuple(row["box"]))
                    require(key not in seen, "Duplicate raw OCR provenance")
                    seen.add(key)
                    source = next(p for p in pages[side] if p["page"] == row["page"])
                    require(any(row["text"] == line["text"] and row["score"] == line["score"] and row["box"] == line["box"]
                                for line in source["ocr"]["lines"]), "Raw OCR provenance does not match source recognition")


def gold_checks(case, result):
    def covered(region, side):
        u0, v0, u1, v1 = region["box"]
        for finding in result["findings"]:
            for location in finding[side]:
                x0, y0, x1, y1 = location["box"]
                area = max(0, min(x1, u1) - max(x0, u0)) * max(0, min(y1, v1) - max(y0, v0))
                if location["page"] == region["page"] and area / max(1, (u1-u0)*(v1-v0)) >= .2:
                    return True
        return False

    edits = case.get("gold_edits", [])
    if not edits and "expected_regions" in case:
        edits = [{side: [region[side]] if region.get(side) else [] for side in ("before", "after")}
                 for region in case["expected_regions"]]
    checks = []
    for edit in edits:
        row = dict(edit)
        for side in ("before", "after"):
            scores = [covered(region, side) for region in edit[side]]
            row[f"{side}_covered"] = any(scores) if edit.get("coverage") == "any_affected_word" else all(scores)
        checks.append(row)
    return checks


def cached_case(case, manifest, study, fingerprints):
    from submissions.services.rapidocr_comparison.engine import compare_pages

    native_path = study / "native.json"
    reference_path = study / "results.json"
    native, reference = read_json(native_path), read_json(reference_path)
    fingerprints[str(native_path)] = sha256(native_path)
    fingerprints[str(reference_path)] = sha256(reference_path)
    before, after = (manifest["documents"][case[side]] for side in ("before", "after"))
    pages = {"before": cached_pages(before, fingerprints), "after": cached_pages(after, fingerprints)}
    result = compare_pages(pages["before"], pages["after"],
                           native[case["before"]], native[case["after"]], profile="broad")
    check_accounting(result)
    check_provenance(result, pages)
    reference_case = next(row for row in reference["cases"] if row["id"] == case["id"])
    expected = migrate_blank_reference(reference_case["results"]["broad"])
    # Compare finding identities/locations and residual limitations, not just totals.
    require(legacy_fields(result) == expected, f"{case['id']}: cached production result differs from frozen study")
    checks = gold_checks(case, result)
    require(checks == reference_case["checks_by_profile"]["broad"], f"{case['id']}: changed gold-location coverage")
    if case["role"] in {"primary_small", "native_adversarial", "row_boundary", "visual_boundary"} and case["expectation"] == "change":
        require(checks and all(row["before_covered"] and row["after_covered"] for row in checks),
                f"{case['id']}: required gold edit not located")
    result["acceptance_gold_checks"] = checks
    return result


def fresh_case(case, manifest):
    from django.db.backends.base.base import BaseDatabaseWrapper
    from submissions.services.pdf_comparison import compare_pdf_bytes

    before, after = (Path(manifest["documents"][case[side]]["path"]).read_bytes()
                     for side in ("before", "after"))
    with patch.object(BaseDatabaseWrapper, "ensure_connection", side_effect=AssertionError("Database access forbidden")):
        result = compare_pdf_bytes(before, after)
    require(result["coverage_status"] in {"complete", "limited"}, "Missing coverage status")
    require(result["coverage_status"] != "limited" or result["coverage_reasons"], "Unexplained limited coverage")
    for side in ("before", "after"):
        require(len(result[f"{side}_pages"]) == len(manifest["documents"][case[side]]["pages"]),
                f"{side}: missing page inventory")
    require(not result["truncated"], "Real sample result was truncated")
    require(len({item["id"] for item in result["findings"]}) == len(result["findings"]), "Duplicate finding IDs")
    for item in result["findings"]:
        for side in ("before", "after"):
            for location in item[side + "_locations"]:
                require(1 <= location["page"] <= len(result[f"{side}_pages"]), "Invalid finding page")
                x0, y0, x1, y1 = location["box"]
                require(0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1, "Invalid normalized finding box")
    if case["role"] == "primary_layout":
        require(result["content_count"] == result["uncertain_count"] == 0, "Layout-only change produced attention findings")
    if case["id"] == "both-blank":
        require(not result["text_available"] and result["uncertain_count"] > 0 and result["coverage_status"] == "limited",
                "Empty OCR was presented as complete")
    if case["expectation"] == "change":
        require(result["content_count"] > 0, "Authored change has no text finding")
    if case["id"] in {"fresh-broken-reference", "change-error"}:
        references = [row for row in result["findings"] if row["category"] == "reference"]
        require(references, "Printed unresolved-reference pattern was not reported")
        marker = "?" if case["id"] == "fresh-broken-reference" else "Error!"
        require(any(marker in row["after_text"] for row in references), "Reference finding omitted recognized evidence")
    if case.get("gold_edits"):
        pixel_findings = []
        for finding in result["findings"]:
            if finding["category"] != "text":
                continue
            converted = {}
            for side in ("before", "after"):
                converted[side] = []
                for loc in finding[side + "_locations"]:
                    page = manifest["documents"][case[side]]["pages"][loc["page"] - 1]
                    converted[side].append({"page": loc["page"], "box": [v * page["width" if i % 2 == 0 else "height"]
                                                                               for i, v in enumerate(loc["box"])]})
            pixel_findings.append(converted)
        checks = gold_checks(case, {"findings": pixel_findings})
        require(all(row["before_covered"] and row["after_covered"] for row in checks), "Fresh OCR missed authored gold location")
        result["acceptance_gold_checks"] = checks
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("both", "cached", "fresh"), default="both")
    parser.add_argument("--all-cached", action="store_true", help="Replay all experiment cases, with gold checks")
    parser.add_argument("--fresh-controls", action="store_true", help="Also recognize the authored PDF controls")
    parser.add_argument("--controls-only", action="store_true", help="Recognize authored controls without repeating real pairs")
    args = parser.parse_args(argv)
    manifest_path, output = args.manifest.resolve(), args.output.resolve()
    manifest = read_json(manifest_path)
    cases = [case for case in manifest["cases"] if case["id"] in {"real1", "real2", "real3"}]
    require({case["id"] for case in cases} == {"real1", "real2", "real3"} and len(cases) == 3,
            "Manifest must contain exactly the three real pairs")
    require(not output.is_relative_to(manifest_path.parent), "Output must not modify the frozen study")
    require(not output.is_relative_to(ROOT / "data"), "Output must not use production data storage")
    output.mkdir(parents=True, exist_ok=False)
    # No project settings, app initialization, production DB, or media directory.
    from django.conf import settings
    if not settings.configured:
        settings.configure(SECRET_KEY="acceptance-only", DATABASES={"default": {
            "ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}, INSTALLED_APPS=["submissions"],
            BASE_DIR=output, MEDIA_ROOT=output / "unused-media", USE_TZ=True)
    import django
    django.setup()

    fingerprints = {str(manifest_path): sha256(manifest_path)}
    fresh_control_ids = {"fresh-insert-word", "fresh-delete-word", "fresh-replace-number",
                         "fresh-broken-reference", "change-error", "fresh-shift-only", "fresh-wrap-only", "both-blank"}
    fresh_cases = ([] if args.controls_only else cases) + ([case for case in manifest["cases"] if case["id"] in fresh_control_ids]
                          if args.fresh_controls or args.controls_only else [])
    cached_cases = manifest["cases"] if args.all_cached else cases
    for case in cached_cases + fresh_cases:
        for side in ("before", "after"):
            document = manifest["documents"][case[side]]
            if "path" in document:
                require(sha256(document["path"]) == document["sha256"], f"Changed source PDF: {document['path']}")
                fingerprints[document["path"]] = document["sha256"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": args.mode,
              "semantic_accuracy": "unadjudicated; candidate differences are not verified defects",
              "python": sys.executable, "worker_python": os.environ.get("CFM_RAPIDOCR_PYTHON", sys.executable),
              "model_dir": os.environ.get("CFM_RAPIDOCR_MODEL_DIR"), "cases": [], "errors": []}
    report["engine_source_sha256"] = {str(path.relative_to(ROOT)): sha256(path)
                                      for path in (ROOT / "submissions/services/rapidocr_comparison").glob("*.py")}
    report["worker_source_sha256"] = sha256(ROOT / "submissions/services/pdf_comparison_worker.py")
    if report["model_dir"]:
        report["model_sha256"] = {path.name: sha256(path) for path in Path(report["model_dir"]).glob("*.onnx")}
    modes = ("cached", "fresh") if args.mode == "both" else (args.mode,)
    for mode in modes:
        for case in cached_cases if mode == "cached" else fresh_cases:
            started = time.monotonic()
            row = {"id": case["id"], "mode": mode, "fresh_ocr": mode == "fresh"}
            try:
                result = (cached_case(case, manifest, manifest_path.parent, fingerprints) if mode == "cached"
                          else fresh_case(case, manifest))
                destination = output / f"{case['id']}-{mode}.json"
                destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
                row.update(status="passed", result=str(destination), result_sha256=sha256(destination))
            except Exception as exc:
                row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                report["errors"].append(row["error"])
            row["seconds"] = round(time.monotonic() - started, 3)
            report["cases"].append(row)
            print(json.dumps(row), flush=True)
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    changed = [path for path, expected in fingerprints.items() if not Path(path).is_file() or sha256(path) != expected]
    if changed:
        report["errors"].append(f"Input integrity failure: {changed}")
    report.update(input_sha256=fingerprints, inputs_unchanged=not changed,
                  passed=not report["errors"], completed_at=datetime.now(timezone.utc).isoformat())
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
