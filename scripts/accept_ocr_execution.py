"""Read-only fresh-PDF execution acceptance; output must be a new disposable file.

Run reference/candidate separately with identical provider/model settings. Compare
full document JSON, not rounded summaries. No Django DB, publication paths or
conference services are used. A pre-change runtime can be supplied for Small parity.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", action="append", required=True, type=Path)
    parser.add_argument("--mode", choices=("legacy", "reference", "candidate"), required=True)
    parser.add_argument("--baseline-runtime", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; do not overwrite acceptance artifacts.")
    from submissions.services.rapidocr_comparison import runtime
    with redirect_stdout(sys.stderr):
        if args.mode == "legacy":
            if not args.baseline_runtime:
                parser.error("Legacy acceptance needs --baseline-runtime.")
            spec = importlib.util.spec_from_file_location("legacy_ocr_runtime", args.baseline_runtime)
            legacy = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(legacy)
            engine, extract = legacy.build_recognizer(), legacy.extract_document
        elif args.mode == "reference":
            engine, extract = runtime._build_recognizer(), runtime._extract_sequential
        else:
            engine, extract = runtime.build_recognizer(), runtime.extract_document
        documents = []
        for path in args.pdf:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            pages, native = extract(data, engine)
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Acceptance input changed during extraction.")
            documents.append({"sha256": digest, "pages": pages, "native": native})
    encoded = json.dumps(documents, sort_keys=True, allow_nan=False).encode()
    with args.output.open("xb") as stream:
        stream.write(encoded)
    print(json.dumps({"documents": len(documents), "pages": sum(len(d["pages"]) for d in documents),
                      "output_sha256": hashlib.sha256(encoded).hexdigest()}))


if __name__ == "__main__":
    main()
