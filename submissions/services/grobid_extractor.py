from dataclasses import dataclass
from pathlib import Path
from urllib import error, request
import uuid
import xml.etree.ElementTree as ET


class GrobidExtractionError(Exception):
    """Raised when GROBID cannot provide usable header metadata."""


@dataclass(frozen=True)
class GrobidExtractionResult:
    title: str
    authors: str
    author_count: int
    raw_tei: str


def check_grobid_api(api_url, timeout_seconds=2):
    endpoint = f"{str(api_url or '').rstrip('/')}/api/isalive"
    if not api_url:
        return {
            "available": False,
            "level": "secondary",
            "label": "No URL",
            "message": "GROBID API URL is empty.",
        }
    req = request.Request(endpoint, method="GET")
    timeout = max(1, min(int(timeout_seconds or 2), 3))
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read(200).decode("utf-8", errors="replace").strip()
    except error.HTTPError as exc:
        return {
            "available": False,
            "level": "danger",
            "label": "Unavailable",
            "message": f"GROBID health check returned HTTP {exc.code}.",
        }
    except error.URLError as exc:
        return {
            "available": False,
            "level": "danger",
            "label": "Unavailable",
            "message": f"GROBID health check failed: {exc.reason}",
        }
    except TimeoutError:
        return {
            "available": False,
            "level": "danger",
            "label": "Timeout",
            "message": "GROBID health check timed out.",
        }
    except Exception as exc:
        return {
            "available": False,
            "level": "danger",
            "label": "Unavailable",
            "message": f"GROBID health check failed: {exc}",
        }

    if status == 200:
        return {
            "available": True,
            "level": "success",
            "label": "Available",
            "message": _health_response_message(body),
        }
    return {
        "available": False,
        "level": "danger",
        "label": "Unavailable",
        "message": f"GROBID health check returned HTTP {status}.",
    }


def _health_response_message(body):
    normalized = str(body or "").strip()
    if not normalized or normalized.lower() in {"true", "1", "ok", "alive"}:
        return "GROBID API is reachable."
    return f"GROBID API is reachable: {normalized}"


def is_grobid_service_unavailable_error(exc):
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, error.HTTPError):
        return cause.code in {408, 429, 502, 503, 504}
    if isinstance(cause, (error.URLError, TimeoutError)):
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in [
            "timed out",
            "connection refused",
            "network is unreachable",
            "temporary failure",
            "name or service not known",
        ]
    )


def extract_header_with_grobid(pdf_path, api_url, timeout_seconds=20):
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise GrobidExtractionError("PDF file does not exist.")

    endpoint = f"{str(api_url).rstrip('/')}/api/processHeaderDocument"
    body, content_type = _multipart_body(pdf_path)
    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": content_type,
            "Accept": "application/xml, text/xml",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=int(timeout_seconds or 20)) as response:
            status = getattr(response, "status", response.getcode())
            payload = response.read()
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GrobidExtractionError(f"GROBID returned HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise GrobidExtractionError(f"GROBID request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GrobidExtractionError("GROBID request timed out.") from exc

    if status != 200:
        raise GrobidExtractionError(f"GROBID returned HTTP {status}.")

    text = payload.decode("utf-8", errors="replace")
    return parse_grobid_tei(text)


def parse_grobid_tei(tei_text):
    try:
        root = ET.fromstring(tei_text)
    except ET.ParseError as exc:
        raise GrobidExtractionError(f"GROBID returned invalid TEI XML: {exc}") from exc

    title = _extract_title(root)
    authors = _extract_authors(root)
    if not title:
        raise GrobidExtractionError("GROBID TEI did not contain a title.")
    if not authors:
        raise GrobidExtractionError("GROBID TEI did not contain authors.")
    return GrobidExtractionResult(
        title=title,
        authors=_format_authors(authors),
        author_count=len(authors),
        raw_tei=tei_text,
    )


def _multipart_body(pdf_path):
    boundary = f"----ConferenceFinalManagerGROBID{uuid.uuid4().hex}"
    filename = pdf_path.name
    pdf_bytes = pdf_path.read_bytes()
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="consolidateHeader"\r\n\r\n',
        b"0\r\n",
        f"--{boundary}\r\n".encode(),
        (
            'Content-Disposition: form-data; name="input"; '
            f'filename="{filename}"\r\n'
        ).encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        pdf_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _extract_title(root):
    preferred_paths = [
        ".//{*}teiHeader/{*}fileDesc/{*}titleStmt/{*}title",
        ".//{*}sourceDesc//{*}analytic/{*}title",
        ".//{*}titlePart",
        ".//{*}title",
    ]
    for path in preferred_paths:
        for element in root.findall(path):
            text = _element_text(element)
            if text:
                return text
    return ""


def _extract_authors(root):
    author_elements = root.findall(".//{*}sourceDesc//{*}analytic/{*}author")
    if not author_elements:
        author_elements = root.findall(".//{*}teiHeader//{*}author")

    authors = []
    for author in author_elements:
        name = _author_name(author)
        if name:
            authors.append(name)
    return authors


def _author_name(author_element):
    pers_name = _first_child(author_element, "persName")
    if pers_name is None:
        return _element_text(author_element)

    forenames = []
    surnames = []
    other_parts = []
    for child in list(pers_name):
        text = _element_text(child)
        if not text:
            continue
        local = _local_name(child.tag)
        if local == "forename":
            forenames.append(text)
        elif local == "surname":
            surnames.append(text)
        elif local not in {"affiliation", "email", "idno"}:
            other_parts.append(text)
    parts = forenames + surnames
    if not parts:
        parts = other_parts or [_element_text(pers_name)]
    return _clean_text(" ".join(parts))


def _first_child(element, local_name):
    for child in element.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _element_text(element):
    return _clean_text(" ".join(element.itertext()))


def _clean_text(value):
    return " ".join(str(value or "").split()).strip(" ,;")


def _format_authors(authors):
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]} and {authors[1]}"
    return f"{', '.join(authors[:-1])}, and {authors[-1]}"


def _local_name(tag):
    return str(tag).split("}", 1)[-1]
