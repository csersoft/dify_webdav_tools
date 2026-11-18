"""Lightweight WebDAV client built on httpx and defusedxml."""

from __future__ import annotations

import fnmatch
import io
import json
import mimetypes
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree as XmlBuilder

import httpx
from defusedxml import ElementTree

DAV_NS = "{DAV:}"
MIB = 1024 * 1024
# Matches the default PLUGIN_MAX_FILE_SIZE of the Dify API service, which caps
# files returned by plugins (download_file blobs).
DEFAULT_MAX_FILE_MB = 50
MAX_FILE_MB_LIMIT = 128  # one transfer peaks at ~2x file size; see manifest resource.memory
MAX_XML_BYTES = 8 * MIB  # 8 MiB multistatus XML response limit
MAX_ERROR_BODY_BYTES = 4096
MAX_DISCARD_BYTES = 64 * 1024
MAX_REDIRECTS = 3
MAX_SCAN_DIRS = 250
MAX_NAME_BYTES = 255
LIST_TIME_BUDGET_SEC = 120.0
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND"})

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b"<resourcetype/><getcontentlength/><getlastmodified/>"
    b"<getcontenttype/><getetag/>"
    b"</prop></propfind>"
)

STATUS_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b"<resourcetype/><quota-available-bytes/><quota-used-bytes/>"
    b"</prop></propfind>"
)

ANY_STATUS = frozenset(range(100, 600))

PROPNAME_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?><propfind xmlns="DAV:"><propname/></propfind>'
)
ALLPROP_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?><propfind xmlns="DAV:"><allprop/></propfind>'
)
DEFAULT_PROP_NS = "urn:dify:webdav-tools"
MAX_PROPS_PER_CALL = 100
MAX_PROP_VALUE_CHARS = 65536
_PROP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]*$")
_CLARK_RE = re.compile(r"^\{([^{}]*)\}(.*)$")
_XML_INVALID_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")

XmlBuilder.register_namespace("d", "DAV:")

GENERIC_MIMES = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/force-download",
        "application/x-download",
    }
)

# Explicit extension-to-MIME map used across listing, stat, upload, download,
# and text writing so MIME inference stays consistent on minimal Docker images
# (without /etc/mime.types) and Windows hosts (where registry entries vary or
# mimetypes.guess_type('.gz') returns None).
EXTRA_MIME_MAP = {
    # Plain text, markup, config & structured data
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".env": "text/plain",
    ".log": "text/plain",
    ".sql": "application/sql",
    # Source code & scripts
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".jsx": "text/jsx",
    ".sh": "application/x-sh",
    ".bash": "application/x-sh",
    ".ps1": "text/plain",
    ".bat": "text/plain",
    ".cmd": "text/plain",
    ".java": "text/x-java-source",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++src",
    ".hpp": "text/x-c++hdr",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".rb": "text/x-ruby",
    ".php": "application/x-httpd-php",
    # Documents & Office formats (important for Dify Document Extractor)
    ".pdf": "application/pdf",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    # Archives & compressed packages
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".bz2": "application/x-bzip2",
    ".tbz2": "application/x-bzip2",
    ".xz": "application/x-xz",
    ".txz": "application/x-xz",
    ".zst": "application/zstd",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    # Common media formats for multimodal Dify workflows
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

STATUS_HINTS = {
    400: "Bad WebDAV request syntax or unsupported property.",
    401: "Authentication failed. Check username, password/token, and auth type.",
    403: "Access denied. Check directory and file permissions on the server.",
    404: "Target resource or directory was not found.",
    405: "Method not allowed for the target resource state.",
    409: "Conflict with target state (for example, parent directory does not exist).",
    412: "Precondition failed (target already exists or ETag did not match).",
    413: "File exceeds the WebDAV server upload size limit.",
    415: "Unsupported media type rejected by the server.",
    423: "Target resource is currently locked on the WebDAV server.",
    502: "Bad gateway (for MOVE/COPY behind a reverse proxy, check X-Forwarded-Proto and Host headers).",
    507: "Insufficient storage space on the WebDAV server.",
}


def _effective_port(scheme: str, port: int | None) -> int:
    """Return explicit port or standard default port for http/https."""
    if port is not None:
        return port
    return 443 if scheme == "https" else 80


def require_param(params: dict[str, Any], name: str) -> Any:
    """Fetch a required tool parameter or raise a descriptive ValueError."""
    value = params.get(name)
    if value is None:
        raise ValueError(f"Parameter '{name}' is required.")
    return value


def require_str(params: dict[str, Any], name: str) -> str:
    """Fetch a required non-empty string parameter or raise ValueError."""
    value = params.get(name)
    if value is None:
        raise ValueError(f"Parameter '{name}' is required.")
    text = str(value).strip()
    if not text:
        raise ValueError(f"Parameter '{name}' cannot be empty.")
    return text


def as_bool(value: Any, default: bool = False) -> bool:
    """Convert heterogeneous Dify parameter values into a boolean."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "off", ""):
            return False
    return bool(value)


def as_int(
    value: Any,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    name: str = "value",
) -> int:
    """Parse an integer parameter and enforce optional bounds."""
    if value is None or (isinstance(value, str) and not value.strip()):
        result = default
    elif isinstance(value, bool):
        result = int(value)
    elif isinstance(value, (int, float)):
        result = int(value)
    elif isinstance(value, str):
        try:
            result = int(float(value.strip()))
        except ValueError as exc:
            raise ValueError(f"Parameter '{name}' must be an integer.") from exc
    else:
        raise ValueError(f"Parameter '{name}' must be an integer.")

    if minimum is not None and result < minimum:
        raise ValueError(f"Parameter '{name}' must be at least {minimum}.")
    if maximum is not None and result > maximum:
        raise ValueError(f"Parameter '{name}' must be at most {maximum}.")
    return result


def normalize_rel_path(path: str | None, *, allow_empty: bool = False) -> str:
    """Validate and normalize a path relative to the configured WebDAV root."""
    raw = (path or "").strip()
    if "://" in raw:
        raise ValueError("Provide a path relative to the WebDAV root, not a full URL.")
    if "\\" in raw:
        raise ValueError("Use forward slashes ('/') instead of backslashes in paths.")
    if any(ord(ch) < 32 for ch in raw):
        raise ValueError("Path contains invalid control characters.")

    stripped = raw.strip("/")
    if not stripped or stripped == ".":
        if allow_empty:
            return ""
        raise ValueError("A non-empty relative path is required.")

    parts: list[str] = []
    for segment in stripped.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ValueError("Parent directory traversal ('..') is not allowed.")
        # Servers fail inconsistently past the common 255-byte filesystem name limit
        # (rclone 405, Apache 403, nginx 500).
        if len(segment.encode("utf-8")) > MAX_NAME_BYTES:
            raise ValueError(
                f"Name '{segment[:40]}...' is longer than {MAX_NAME_BYTES} bytes (UTF-8), "
                "which most WebDAV servers and filesystems reject."
            )
        parts.append(segment)

    if not parts:
        if allow_empty:
            return ""
        raise ValueError("A non-empty relative path is required.")
    return "/".join(parts)


def encode_rel_path(rel_path: str) -> str:
    """Percent-encode each segment of an already normalized relative path."""
    if not rel_path:
        return ""
    return "/".join(quote(part, safe="") for part in rel_path.split("/"))


def guess_mime_type(path: str, reported: str | None = None) -> str:
    """Resolve MIME type from server header with filename extension fallback."""
    cleaned = (reported or "").split(";", 1)[0].strip().lower()
    if cleaned and cleaned not in GENERIC_MIMES:
        return cleaned

    lower_path = path.lower()
    dot_idx = lower_path.rfind(".")
    if dot_idx != -1:
        ext = lower_path[dot_idx:]
        if ext in EXTRA_MIME_MAP:
            return EXTRA_MIME_MAP[ext]

    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def read_limited(response: httpx.Response, limit: int) -> bytes:
    """Stream response bytes up to `limit` bytes."""
    # BytesIO.getvalue() hands over its internal buffer instead of copying it,
    # which keeps peak memory near 1x the body size.
    buffer = io.BytesIO()
    size = 0
    for chunk in response.iter_bytes(chunk_size=65536):
        size += len(chunk)
        if size > limit:
            limit_mib = limit / (1024 * 1024)
            raise ValueError(f"Response body exceeds the {limit_mib:.0f} MiB limit.")
        buffer.write(chunk)
    return buffer.getvalue()


def discard_body(response: httpx.Response) -> None:
    """Drain a small unused body; closing an unread response drops the keep-alive connection."""
    size = 0
    try:
        for chunk in response.iter_bytes(chunk_size=65536):
            size += len(chunk)
            if size > MAX_DISCARD_BYTES:
                break
    except httpx.HTTPError:
        pass


def etag_core(etag: str | None) -> str:
    """Strip the weak prefix and quotes so ETags from PROPFIND and GET compare equal."""
    value = (etag or "").strip()
    if value[:2].upper() == "W/":
        value = value[2:]
    return value.strip().strip('"')


def _error_detail(response: httpx.Response) -> str:
    """Extract a short human-readable message from an error response body."""
    raw = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=MAX_ERROR_BODY_BYTES):
            raw.extend(chunk)
            if len(raw) >= MAX_ERROR_BODY_BYTES:
                break
    except httpx.HTTPError:
        pass
    body = bytes(raw[:MAX_ERROR_BODY_BYTES]).decode("utf-8", errors="replace").strip()
    if not body:
        return ""

    text = ""
    if body.startswith("<"):
        try:
            text = " ".join(ElementTree.fromstring(body, forbid_dtd=True).itertext())
        except Exception:
            text = re.sub(r"<[^>]+>", " ", body)
    else:
        text = body
    text = " ".join(text.split())
    if not text or text.lower() == (response.reason_phrase or "").lower():
        return ""
    return text[:300]


class WebDAVConnectionError(ValueError):
    """The server could not be reached (DNS, TCP, TLS, or timeout failure)."""


def parse_dav_header(value: str | None) -> tuple[list[str], list[str]]:
    """Split a DAV response header into compliance classes and extension tokens."""
    classes: list[str] = []
    extensions: list[str] = []
    for token in (value or "").split(","):
        token = token.strip().strip("<>").strip()
        if not token:
            continue
        target = classes if token.isdigit() else extensions
        if token not in target:
            target.append(token)
    return classes, extensions


def parse_server_header(value: str | None) -> tuple[str, str]:
    """Return (product, version) from the first 'name/version' token of a Server header."""
    tokens = (value or "").split()
    if not tokens:
        return "", ""
    product, _, version = tokens[0].partition("/")
    return product, version


PropName = tuple[str, str]  # (namespace, local name)


def resolve_prop_name(key: Any, namespace: str, *, allow_dav: bool) -> PropName:
    """Turn a plain name (uses `namespace`) or Clark '{ns}name' into (ns, name)."""
    text = str(key).strip()
    match = _CLARK_RE.match(text)
    ns, name = (match.group(1).strip(), match.group(2).strip()) if match else (namespace, text)
    if not ns:
        raise ValueError(f"Property '{text}' has an empty namespace.")
    if ns == "DAV:" and not allow_dav:
        raise ValueError(
            f"Property '{name}' is in the server-managed 'DAV:' namespace and cannot be changed."
        )
    if not _PROP_NAME_RE.match(name):
        raise ValueError(
            f"Invalid property name '{name}'. Use letters, digits, '_', '-' or '.', "
            "starting with a letter or '_'."
        )
    return ns, name


def prop_key(prop: PropName, namespace: str) -> str:
    """Output key: plain name inside the default namespace, Clark notation otherwise."""
    ns, name = prop
    return name if ns == namespace else f"{{{ns}}}{name}"


def parse_prop_values(raw: Any, namespace: str) -> dict[PropName, str]:
    """Parse a JSON object (or dict) of property name -> scalar value."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    error = 'Parameter \'properties\' must be a JSON object such as {"category": "report"}.'
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(error) from exc
    if not isinstance(raw, dict):
        raise ValueError(error)

    values: dict[PropName, str] = {}
    for key, value in raw.items():
        if value is None or isinstance(value, (dict, list)):
            raise ValueError(f"Value of property '{key}' must be a string, number, or boolean.")
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if len(text) > MAX_PROP_VALUE_CHARS:
            raise ValueError(
                f"Value of property '{key}' exceeds {MAX_PROP_VALUE_CHARS} characters."
            )
        if _XML_INVALID_CHARS.search(text):
            raise ValueError(f"Value of property '{key}' contains characters not allowed in XML.")
        values[resolve_prop_name(key, namespace, allow_dav=False)] = text
    return values


def parse_prop_names(raw: Any, namespace: str, *, allow_dav: bool) -> list[PropName]:
    """Parse a JSON array or comma/newline separated list of property names."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                items = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("Property name list is not a valid JSON array.") from exc
        else:
            items = re.split(r"[,\n]", text)
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("Property names must be a JSON array or a comma-separated list.")

    names: list[PropName] = []
    for item in items:
        if not str(item).strip():
            continue
        prop = resolve_prop_name(item, namespace, allow_dav=allow_dav)
        if prop not in names:
            names.append(prop)
    return names


def _prop_tag(prop: PropName) -> str:
    return f"{{{prop[0]}}}{prop[1]}"


def _prop_value(elem: Any) -> str:
    """Text of a property element, or its inner XML for structured values."""
    if len(elem) == 0:
        return elem.text or ""
    return (elem.text or "") + "".join(
        XmlBuilder.tostring(child, encoding="unicode") for child in elem
    )


def _build_propfind(names: list[PropName]) -> bytes:
    root = XmlBuilder.Element("{DAV:}propfind")
    prop = XmlBuilder.SubElement(root, "{DAV:}prop")
    for name in names:
        XmlBuilder.SubElement(prop, _prop_tag(name))
    return XmlBuilder.tostring(root, encoding="utf-8", xml_declaration=True)


def _build_propertyupdate(to_set: dict[PropName, str], to_remove: list[PropName]) -> bytes:
    root = XmlBuilder.Element("{DAV:}propertyupdate")
    if to_set:
        prop = XmlBuilder.SubElement(XmlBuilder.SubElement(root, "{DAV:}set"), "{DAV:}prop")
        for name, value in to_set.items():
            XmlBuilder.SubElement(prop, _prop_tag(name)).text = value
    if to_remove:
        prop = XmlBuilder.SubElement(XmlBuilder.SubElement(root, "{DAV:}remove"), "{DAV:}prop")
        for name in to_remove:
            XmlBuilder.SubElement(prop, _prop_tag(name))
    return XmlBuilder.tostring(root, encoding="utf-8", xml_declaration=True)


def emit_result(tool: Any, payload: dict[str, Any], summary: str):
    """Yield English text summary, JSON object, and root output_schema variables."""
    yield tool.create_text_message(summary)
    yield tool.create_json_message(payload)
    for key, value in payload.items():
        yield tool.create_variable_message(key, value)


class WebDAV:
    """HTTP/XML WebDAV client supporting Basic, Digest, and Bearer authentication."""

    def __init__(self, credentials: dict[str, Any]) -> None:
        base_url = (credentials.get("base_url") or "").strip()
        if not base_url:
            raise ValueError("WebDAV base URL is required.")

        self.root = base_url.rstrip("/") + "/"
        parsed = urlsplit(self.root)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Invalid WebDAV base URL. Provide an http(s) directory URL without "
                "embedded credentials, query parameters, or fragments."
            )

        self._origin = (
            parsed.scheme,
            parsed.hostname.lower(),
            _effective_port(parsed.scheme, parsed.port),
        )
        self.auth_type = (credentials.get("auth_type") or "basic").strip().lower()
        username = (credentials.get("username") or "").strip()
        password = credentials.get("password") or ""

        self._auth: httpx.Auth | None = None
        self._default_headers: dict[str, str] = {}

        if self.auth_type == "basic":
            if not username or not password:
                raise ValueError("Username and password are required for Basic authentication.")
            self._auth = httpx.BasicAuth(username, password)
        elif self.auth_type == "digest":
            if not username or not password:
                raise ValueError("Username and password are required for Digest authentication.")
            self._auth = httpx.DigestAuth(username, password)
        elif self.auth_type == "bearer":
            token = password.strip()
            if not token:
                raise ValueError("Token (in the password field) is required for Bearer authentication.")
            self._default_headers["Authorization"] = f"Bearer {token}"
        else:
            raise ValueError(f"Unsupported authentication type: '{self.auth_type}'.")

        self.verify_ssl = as_bool(credentials.get("verify_ssl"), default=True)
        timeout_sec = as_int(
            credentials.get("timeout"),
            default=30,
            minimum=5,
            maximum=300,
            name="timeout",
        )
        self.timeout = httpx.Timeout(float(timeout_sec), connect=min(float(timeout_sec), 15.0))
        max_file_mb = as_int(
            credentials.get("max_file_size_mb"),
            default=DEFAULT_MAX_FILE_MB,
            minimum=1,
            maximum=MAX_FILE_MB_LIMIT,
            name="max_file_size_mb",
        )
        self.max_file_bytes = max_file_mb * MIB
        self.read_only = as_bool(credentials.get("read_only"), default=False)
        self._client: httpx.Client | None = None

    def _require_writable(self, operation: str) -> None:
        if self.read_only:
            raise ValueError(
                f"{operation} is disabled because WebDAV Tools is configured in read-only "
                "mode. Turn off Read-Only Mode in the provider credentials to allow changes."
            )

    def __enter__(self) -> WebDAV:
        self._ensure_client()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                auth=self._auth,
                headers=self._default_headers,
                verify=self.verify_ssl,
                timeout=self.timeout,
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        """Close underlying HTTP connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def dir_url(self, rel_path: str) -> str:
        """Build URL for a collection (always trailing slash)."""
        encoded = encode_rel_path(rel_path)
        return self.root + (encoded + "/" if encoded else "")

    def item_url(self, rel_path: str) -> str:
        """Build URL for a file or generic resource (no trailing slash unless root)."""
        encoded = encode_rel_path(rel_path)
        return self.root + encoded if encoded else self.root

    def _validate_redirect_target(self, current_url: str, location: str) -> str:
        next_url = urljoin(current_url, location)
        current_parts = urlsplit(current_url)
        next_parts = urlsplit(next_url)

        if next_parts.scheme not in ("http", "https") or not next_parts.hostname:
            raise ValueError("Server returned an invalid redirect URL.")
        if current_parts.scheme == "https" and next_parts.scheme != "https":
            raise ValueError("Refusing insecure redirect from HTTPS to HTTP.")

        next_host = next_parts.hostname.lower()
        next_port = _effective_port(next_parts.scheme, next_parts.port)
        same_port = next_port == self._origin[2]
        https_upgrade = (
            current_parts.scheme == "http"
            and next_parts.scheme == "https"
            and self._origin[2] == 80
            and next_port == 443
        )
        if next_host != self._origin[1] or not (same_port or https_upgrade):
            raise ValueError("Refusing cross-origin redirect from WebDAV server.")
        return next_url

    def request(
        self,
        method: str,
        url: str,
        *,
        statuses: set[int],
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        limit: int = 0,
    ) -> tuple[bytes, httpx.Headers, int, str]:
        """Execute a WebDAV HTTP request with safe same-origin redirect handling."""
        if method not in READ_ONLY_METHODS:
            self._require_writable(f"HTTP {method}")
        client = self._ensure_client()
        target_url = url

        try:
            for _ in range(MAX_REDIRECTS + 1):
                with client.stream(
                    method,
                    target_url,
                    headers=headers,
                    content=content,
                ) as response:
                    if response.status_code in (301, 302, 307, 308):
                        location = response.headers.get("location")
                        if location:
                            target_url = self._validate_redirect_target(target_url, location)
                            continue

                    if response.status_code not in statuses:
                        hint = STATUS_HINTS.get(
                            response.status_code,
                            "Check the server WebDAV configuration and logs.",
                        )
                        detail = _error_detail(response)
                        server_note = f" Server message: {detail}" if detail else ""
                        raise ValueError(
                            f"{method} failed with HTTP {response.status_code}. {hint}{server_note}"
                        )

                    if limit > 0:
                        data = read_limited(response, limit)
                    else:
                        discard_body(response)
                        data = b""
                    return data, response.headers, response.status_code, target_url

            raise ValueError(f"{method} exceeded maximum redirects ({MAX_REDIRECTS}).")
        except httpx.RequestError as exc:
            raise WebDAVConnectionError(
                f"WebDAV connection error ({type(exc).__name__}): {exc}. "
                "Verify network reachability, base URL, and TLS certificate settings."
            ) from None

    @staticmethod
    def _parse_multistatus(data: bytes) -> ElementTree.Element:
        try:
            root = ElementTree.fromstring(data, forbid_dtd=True)
        except Exception as exc:
            raise ValueError("Server returned invalid WebDAV XML response.") from exc
        if root.tag != DAV_NS + "multistatus":
            raise ValueError("Server did not return a WebDAV 207 multistatus XML document.")
        return root

    @staticmethod
    def _extract_props(response_elem: ElementTree.Element) -> dict[str, ElementTree.Element]:
        props: dict[str, ElementTree.Element] = {}
        for group in response_elem.findall(DAV_NS + "propstat"):
            status_parts = (group.findtext(DAV_NS + "status") or "").split()
            prop = group.find(DAV_NS + "prop")
            if len(status_parts) >= 2 and status_parts[1] == "200" and prop is not None:
                for node in prop:
                    props[node.tag] = node
        return props

    @staticmethod
    def _build_entry(
        rel_path: str,
        name: str,
        props: dict[str, ElementTree.Element],
    ) -> dict[str, Any] | None:
        resource = props.get(DAV_NS + "resourcetype")
        if resource is None:
            return None

        is_dir = resource.find(DAV_NS + "collection") is not None
        size_node = props.get(DAV_NS + "getcontentlength")
        raw_size = (size_node.text or "").strip() if size_node is not None else ""
        size = int(raw_size) if (not is_dir and raw_size.isdigit()) else 0

        modified_node = props.get(DAV_NS + "getlastmodified")
        last_modified = (modified_node.text or "").strip() if modified_node is not None else ""

        etag_node = props.get(DAV_NS + "getetag")
        etag = (etag_node.text or "").strip() if etag_node is not None else ""

        ctype_node = props.get(DAV_NS + "getcontenttype")
        raw_ctype = (ctype_node.text or "").strip() if ctype_node is not None else ""
        content_type = "" if is_dir else guess_mime_type(name or rel_path, raw_ctype)

        return {
            "name": name,
            "path": rel_path,
            "type": "directory" if is_dir else "file",
            "size": size,
            "content_type": content_type,
            "etag": etag,
            "last_modified": last_modified,
        }

    @classmethod
    def _check_multistatus_errors(cls, data: bytes, method: str) -> None:
        """Raise ValueError if a 207 response from MOVE/COPY/DELETE reports sub-errors."""
        if not data.strip():
            return
        root = cls._parse_multistatus(data)
        for item in root.findall(DAV_NS + "response"):
            href = (item.findtext(DAV_NS + "href") or "").strip()
            statuses = [
                (el.text or "").strip()
                for el in item.findall(".//" + DAV_NS + "status")
            ]
            for status_line in statuses:
                parts = status_line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    code = int(parts[1])
                    if code >= 400 and code != 424:
                        hint = STATUS_HINTS.get(code, "")
                        target = f" on '{unquote(href)}'" if href else ""
                        raise ValueError(
                            f"{method} reported HTTP {code}{target}. {hint}".strip()
                        )

    def server_status(self) -> dict[str, Any]:
        """Probe the root with OPTIONS + PROPFIND; report health instead of raising."""
        result: dict[str, Any] = {
            "status": "unreachable",
            "reachable": False,
            "authenticated": False,
            "message": "",
            "base_url": self.root,
            "effective_url": self.root,
            "http_status": 0,
            "response_time_ms": 0.0,
            "options_ms": 0.0,
            "propfind_ms": 0.0,
            "server": "",
            "server_product": "",
            "server_version": "",
            "dav_classes": [],
            "dav_extensions": [],
            "allowed_methods": [],
            "supports_locking": False,
            "quota_available_bytes": -1,
            "quota_used_bytes": -1,
            "tls": self.root.startswith("https://"),
            "verify_ssl": self.verify_ssl,
            "read_only": self.read_only,
            "max_file_size_mb": self.max_file_bytes // MIB,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        def record_server(headers: httpx.Headers) -> None:
            server = (headers.get("server") or "").strip()
            if server and not result["server"]:
                result["server"] = server
                result["server_product"], result["server_version"] = parse_server_header(server)

        try:
            # OPTIONS also pays for connection setup (TCP/TLS), PROPFIND reuses it.
            started = time.perf_counter()
            _, opt_headers, _, _ = self.request("OPTIONS", self.root, statuses=ANY_STATUS)
            result["options_ms"] = round((time.perf_counter() - started) * 1000, 1)
            result["reachable"] = True
            record_server(opt_headers)
            classes, extensions = parse_dav_header(opt_headers.get("dav"))
            allowed = [
                m.strip().upper() for m in (opt_headers.get("allow") or "").split(",") if m.strip()
            ]
            result.update(dav_classes=classes, dav_extensions=extensions, allowed_methods=allowed)
            result["supports_locking"] = "2" in classes or "LOCK" in allowed

            started = time.perf_counter()
            data, pf_headers, pf_status, effective_url = self.request(
                "PROPFIND",
                self.root,
                statuses=ANY_STATUS,
                headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                content=STATUS_PROPFIND_BODY,
                limit=MAX_XML_BYTES,
            )
            result["propfind_ms"] = round((time.perf_counter() - started) * 1000, 1)
        except WebDAVConnectionError as exc:
            result["message"] = str(exc)
            return self._finish_status(result)
        except ValueError as exc:
            result.update(status="error", reachable=True, message=str(exc))
            return self._finish_status(result)

        record_server(pf_headers)
        result["http_status"] = pf_status
        result["effective_url"] = effective_url

        if pf_status in (401, 403, 404):
            result["status"] = {401: "unauthorized", 403: "forbidden", 404: "not_found"}[pf_status]
            result["message"] = f"PROPFIND returned HTTP {pf_status}. {STATUS_HINTS[pf_status]}"
        elif pf_status in (405, 501):
            result["status"] = "not_webdav"
            result["message"] = (
                f"PROPFIND returned HTTP {pf_status}; the URL does not appear to be a "
                "WebDAV endpoint."
            )
        elif pf_status != 207:
            result["status"] = "error"
            hint = STATUS_HINTS.get(pf_status, "Check the server WebDAV configuration and logs.")
            result["message"] = f"PROPFIND returned HTTP {pf_status}. {hint}"
        else:
            self._apply_root_props(result, data)
        return self._finish_status(result)

    def _apply_root_props(self, result: dict[str, Any], data: bytes) -> None:
        try:
            root = self._parse_multistatus(data)
        except ValueError as exc:
            result.update(status="not_webdav", message=str(exc))
            return
        response = root.find(DAV_NS + "response")
        props = self._extract_props(response) if response is not None else {}
        resource = props.get(DAV_NS + "resourcetype")
        if resource is None or resource.find(DAV_NS + "collection") is None:
            result.update(
                status="error",
                authenticated=True,
                message="The configured WebDAV root URL is not a directory (collection).",
            )
            return

        for key, prop in (
            ("quota_available_bytes", "quota-available-bytes"),
            ("quota_used_bytes", "quota-used-bytes"),
        ):
            node = props.get(DAV_NS + prop)
            text = (node.text or "").strip() if node is not None else ""
            if text.isdigit():
                result[key] = int(text)
        result.update(status="ok", authenticated=True, message="WebDAV server is healthy.")

    @staticmethod
    def _finish_status(result: dict[str, Any]) -> dict[str, Any]:
        result["response_time_ms"] = round(result["options_ms"] + result["propfind_ms"], 1)
        return result

    def stat(self, path: str = "", *, resolve_etag: bool = False) -> dict[str, Any]:
        """Inspect a file or directory without raising an error when not found.

        With resolve_etag, a file whose PROPFIND lacks getetag gets its ETag via HEAD.
        """
        rel_path = normalize_rel_path(path, allow_empty=True)
        url = self.item_url(rel_path) if rel_path else self.root
        name = rel_path.rsplit("/", 1)[-1] if rel_path else "/"

        data, _, status_code, _ = self.request(
            "PROPFIND",
            url,
            statuses={207, 400, 404, 410},
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
            content=PROPFIND_BODY,
            limit=MAX_XML_BYTES,
        )

        if status_code == 400:
            # Apache mod_dav answers 400 for a path below a file; that path cannot exist.
            parent = rel_path.rpartition("/")[0]
            if not parent or self.stat(parent)["type"] == "directory":
                raise ValueError(f"PROPFIND failed with HTTP 400. {STATUS_HINTS[400]}")
            status_code = 404

        if status_code in (404, 410):
            return {
                "exists": False,
                "path": rel_path,
                "name": name,
                "type": "",
                "size": 0,
                "content_type": "",
                "etag": "",
                "last_modified": "",
            }

        root = self._parse_multistatus(data)
        for item in root.findall(DAV_NS + "response"):
            props = self._extract_props(item)
            entry = self._build_entry(rel_path, name, props)
            if entry is not None:
                if resolve_etag and entry["type"] == "file" and not entry["etag"]:
                    entry["etag"] = self._head_etag(rel_path)
                return {"exists": True, **entry}

        raise ValueError(f"Server did not return WebDAV properties for '{rel_path or '/'}'.")

    def _head_etag(self, rel_path: str) -> str:
        """ETag from a HEAD response; nginx's dav-ext module omits getetag in PROPFIND."""
        _, headers, status_code, _ = self.request(
            "HEAD", self.item_url(rel_path), statuses=ANY_STATUS
        )
        return (headers.get("etag") or "").strip() if status_code == 200 else ""

    def _list_single_dir(self, rel_path: str) -> list[dict[str, Any]]:
        """List immediate children of a single directory via Depth: 1 PROPFIND."""
        url = self.dir_url(rel_path)
        data, _, status_code, effective_url = self.request(
            "PROPFIND",
            url,
            statuses={207, 400},
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
            content=PROPFIND_BODY,
            limit=MAX_XML_BYTES,
        )
        if status_code == 400:
            # Apache mod_dav rejects a collection URL (trailing slash) that names a file.
            info = self.stat(rel_path)
            if info["exists"] and info["type"] == "file":
                raise ValueError(f"Path '{rel_path}' is a file, not a directory.")
            if not info["exists"]:
                raise ValueError(f"Directory '{rel_path or '/'}' does not exist.")
            raise ValueError(f"PROPFIND failed with HTTP 400. {STATUS_HINTS[400]}")

        root = self._parse_multistatus(data)
        req_path = unquote(urlsplit(effective_url).path).rstrip("/")

        parsed_items: list[tuple[str, dict[str, ElementTree.Element], bool]] = []
        for item in root.findall(DAV_NS + "response"):
            href = (item.findtext(DAV_NS + "href") or "").strip()
            if not href:
                continue
            href_path = unquote(urlsplit(urljoin(effective_url, href)).path).rstrip("/")
            props = self._extract_props(item)
            resource = props.get(DAV_NS + "resourcetype")
            if resource is None:
                continue
            is_dir = resource.find(DAV_NS + "collection") is not None
            parsed_items.append((href_path, props, is_dir))

        if not parsed_items:
            raise ValueError(
                f"Directory '{rel_path or '/'}' does not exist or returned no properties."
            )

        # Match self collection directly first; fall back to shortest common prefix
        # when behind a path-rewriting reverse proxy.
        self_idx: int | None = None
        for idx, (href_path, _, _) in enumerate(parsed_items):
            if href_path == req_path:
                self_idx = idx
                break

        if self_idx is None:
            candidates = [
                (idx, href_path)
                for idx, (href_path, _, is_dir) in enumerate(parsed_items)
                if is_dir
            ]
            candidates.sort(key=lambda pair: len(pair[1]))
            for idx, cand_path in candidates:
                prefix = cand_path + "/"
                if all(
                    other == cand_path or other.startswith(prefix)
                    for other, _, _ in parsed_items
                ):
                    self_idx = idx
                    break

        if self_idx is None:
            raise ValueError(
                f"Directory '{rel_path or '/'}' was not found in WebDAV multistatus response."
            )

        base_href, _, self_is_dir = parsed_items[self_idx]
        if not self_is_dir:
            raise ValueError(f"Path '{rel_path}' is a file, not a directory.")

        base_prefix = base_href + "/"
        entries: list[dict[str, Any]] = []
        for idx, (href_path, props, _) in enumerate(parsed_items):
            if idx == self_idx or not href_path.startswith(base_prefix):
                continue
            child_name = href_path[len(base_prefix) :]
            if not child_name or "/" in child_name:
                continue
            child_rel = f"{rel_path}/{child_name}" if rel_path else child_name
            entry = self._build_entry(child_rel, child_name, props)
            if entry is not None:
                entries.append(entry)

        entries.sort(key=lambda item: (0 if item["type"] == "directory" else 1, item["name"].lower()))
        return entries

    def directory(self, path: str = "", depth: int = 1) -> list[dict[str, Any]]:
        """Verify directory (depth=0) or list immediate children (depth=1)."""
        rel_path = normalize_rel_path(path, allow_empty=True)
        if depth == 0:
            info = self.stat(rel_path)
            if not info["exists"]:
                raise ValueError(f"Directory '{rel_path or '/'}' does not exist.")
            if info["type"] != "directory":
                raise ValueError(f"Configured path '{rel_path or '/'}' is not a directory.")
            return []
        return self._list_single_dir(rel_path)

    def list_directory(
        self,
        path: str = "",
        *,
        recursive: bool = False,
        max_depth: int = 3,
        pattern: str = "",
        entry_type: str = "all",
        limit: int = 200,
    ) -> dict[str, Any]:
        """List directory contents with optional recursion, glob filter, and item cap."""
        rel_path = normalize_rel_path(path, allow_empty=True)
        entry_filter = (entry_type or "all").strip().lower()
        if entry_filter not in ("all", "file", "directory"):
            raise ValueError("Parameter 'entry_type' must be 'all', 'file', or 'directory'.")

        glob_pat = (pattern or "").strip().lower()
        results: list[dict[str, Any]] = []
        truncated_reason = ""

        queue: deque[tuple[str, int]] = deque([(rel_path, 1)])
        visited: set[str] = {rel_path}
        scanned_dirs = 0
        deadline = time.monotonic() + LIST_TIME_BUDGET_SEC

        while queue:
            current_dir, current_depth = queue.popleft()
            scanned_dirs += 1
            if scanned_dirs > MAX_SCAN_DIRS:
                truncated_reason = "max_scan_dirs"
                break
            if scanned_dirs > 1 and time.monotonic() > deadline:
                truncated_reason = "time_budget"
                break

            children = self._list_single_dir(current_dir)
            for child in children:
                if (
                    recursive
                    and child["type"] == "directory"
                    and current_depth < max_depth
                    and child["path"] not in visited
                ):
                    visited.add(child["path"])
                    queue.append((child["path"], current_depth + 1))

                if entry_filter != "all" and child["type"] != entry_filter:
                    continue
                if glob_pat and not (
                    fnmatch.fnmatch(child["name"].lower(), glob_pat)
                    or fnmatch.fnmatch(child["path"].lower(), glob_pat)
                ):
                    continue

                if len(results) >= limit:
                    truncated_reason = "limit"
                    queue.clear()
                    break
                results.append(child)

        return {
            "path": rel_path,
            "entries": results,
            "count": len(results),
            "truncated": bool(truncated_reason),
            "truncated_reason": truncated_reason,
        }

    def create_directory(self, path: str, *, parents: bool = True) -> dict[str, Any]:
        """Create a WebDAV directory via MKCOL, optionally creating parent folders."""
        self._require_writable("Creating directories")
        rel_path = normalize_rel_path(path, allow_empty=False)
        existing = self.stat(rel_path)
        if existing["exists"]:
            if existing["type"] != "directory":
                raise ValueError(
                    f"Cannot create directory '{rel_path}': a file already exists at that path."
                )
            return {"path": rel_path, "created": False, "already_exists": True}

        # RFC 4918 mandates 409 for a missing parent, but some servers answer 403/404,
        # and Apache mod_dav answers 400 when an ancestor is a file.
        allowed = {201, 405, 400, 403, 404, 409} if (parents and "/" in rel_path) else {201, 405}
        _, _, status_code, _ = self.request("MKCOL", self.dir_url(rel_path), statuses=allowed)
        if status_code == 201:
            return {"path": rel_path, "created": True, "already_exists": False}
        if status_code == 405:
            recheck = self.stat(rel_path)
            if recheck["exists"] and recheck["type"] == "directory":
                return {"path": rel_path, "created": False, "already_exists": True}
            # Some servers (e.g. rclone) also answer 405 when an ancestor is a file;
            # with parents=True the walk below reports which segment is blocking.
            if not parents or "/" not in rel_path:
                raise ValueError(
                    f"Failed to create directory '{rel_path}' (HTTP 405). {STATUS_HINTS[405]}"
                )

        segments = rel_path.split("/")
        current = ""
        for segment in segments:
            current = f"{current}/{segment}" if current else segment
            st = self.stat(current)
            if st["exists"]:
                if st["type"] != "directory":
                    raise ValueError(
                        f"Cannot create directory '{rel_path}': '{current}' is an existing file."
                    )
                continue
            _, _, code, _ = self.request("MKCOL", self.dir_url(current), statuses={201, 405})
            self._confirm_mkcol(current, code)

        return {"path": rel_path, "created": True, "already_exists": False}

    def _confirm_mkcol(self, rel_path: str, status_code: int) -> None:
        """Treat MKCOL 405 as success only if a directory now exists (concurrent creation)."""
        if status_code != 405:
            return
        recheck = self.stat(rel_path)
        if not (recheck["exists"] and recheck["type"] == "directory"):
            raise ValueError(
                f"Failed to create directory '{rel_path}' (HTTP 405). "
                f"{STATUS_HINTS[405]}"
            )

    def _prepare_parent(self, rel_path: str, target: dict[str, Any], create_parents: bool) -> bool:
        """Create missing parents, or verify they exist when creation is disabled."""
        if target["exists"]:
            return False
        if create_parents:
            return self._ensure_parent_dir(rel_path)
        # nginx creates missing parents on PUT/MOVE/COPY regardless, so check here.
        parent = rel_path.rpartition("/")[0]
        if parent:
            info = self.stat(parent)
            if not (info["exists"] and info["type"] == "directory"):
                raise ValueError(
                    f"Parent directory '{parent}' does not exist and create_parents is disabled."
                )
        return False

    def _ensure_parent_dir(self, rel_path: str) -> bool:
        if "/" not in rel_path:
            return False
        parent_dir = rel_path.rsplit("/", 1)[0]
        res = self.create_directory(parent_dir, parents=True)
        return bool(res["created"])

    def _check_size(self, size: int) -> None:
        if size > self.max_file_bytes:
            raise ValueError(
                f"File size ({size} bytes) exceeds the {self.max_file_bytes // MIB} MiB "
                "single-file limit set in the provider's Max File Size setting."
            )

    def download(
        self,
        path: str,
        *,
        _existing: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """Download a WebDAV file up to max_file_bytes and return bytes + metadata."""
        rel_path = normalize_rel_path(path, allow_empty=False)
        info = _existing if _existing is not None else self.stat(rel_path)
        if not info["exists"]:
            raise ValueError(f"File '{rel_path}' does not exist.")
        if info["type"] == "directory":
            raise ValueError(f"Path '{rel_path}' is a directory, not a file.")
        self._check_size(info["size"])

        data, headers, _, _ = self.request(
            "GET",
            self.item_url(rel_path),
            statuses={200},
            limit=self.max_file_bytes,
        )
        filename = rel_path.rsplit("/", 1)[-1]
        mime_type = guess_mime_type(filename, headers.get("content-type") or info["content_type"])
        etag = (headers.get("etag") or info["etag"] or "").strip()
        last_modified = (headers.get("last-modified") or info["last_modified"] or "").strip()

        meta = {
            "path": rel_path,
            "filename": filename,
            "mime_type": mime_type,
            "size": len(data),
            "etag": etag,
            "last_modified": last_modified,
        }
        return data, meta

    def _put_bytes(
        self,
        rel_path: str,
        data: bytes,
        *,
        content_type: str,
        overwrite: bool,
        create_parents: bool,
        if_match_etag: str = "",
        _existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._check_size(len(data))

        existing = _existing if _existing is not None else self.stat(rel_path)
        if existing["exists"] and existing["type"] == "directory":
            raise ValueError(f"Target path '{rel_path}' is an existing directory, not a file.")

        # Some servers (e.g. rclone) ignore If-Match / If-None-Match on PUT, so the
        # preconditions are also checked client-side.
        expected_etag = if_match_etag.strip()
        if expected_etag:
            if not existing["exists"]:
                raise ValueError(
                    f"Precondition failed: target '{rel_path}' does not exist for If-Match."
                )
            remote_etag = existing["etag"] or self._head_etag(rel_path)
            if not remote_etag:
                raise ValueError(
                    f"Cannot check If-Match for '{rel_path}': the server does not report an "
                    "ETag for this file."
                )
            if etag_core(remote_etag) != etag_core(expected_etag):
                raise ValueError(
                    f"ETag mismatch for '{rel_path}': remote ETag is {remote_etag} "
                    "(the file was modified since it was read)."
                )
        elif not overwrite and existing["exists"]:
            raise ValueError(
                f"Target file '{rel_path}' already exists and overwrite is disabled."
            )

        created_parents = self._prepare_parent(rel_path, existing, create_parents)

        headers = {"Content-Type": content_type}
        if expected_etag:
            # If-Match uses strong comparison, so a weak ETag always fails with 412. The
            # current remote ETag already matched above; once it is strong (Apache keeps
            # ETags weak for ~1s after a write) it lets the server re-check atomically.
            if not remote_etag.upper().startswith("W/"):
                headers["If-Match"] = f'"{etag_core(remote_etag)}"'
        elif not overwrite:
            headers["If-None-Match"] = "*"

        _, resp_headers, _, _ = self.request(
            "PUT",
            self.item_url(rel_path),
            statuses={200, 201, 204},
            headers=headers,
            content=data,
        )
        etag = (resp_headers.get("etag") or "").strip()
        if not etag:
            # Some servers (e.g. Apache mod_dav) send no ETag on PUT; the result's ETag
            # must still be usable for a later If-Match.
            etag = self.stat(rel_path, resolve_etag=True)["etag"]
        return {
            "path": rel_path,
            "size": len(data),
            "content_type": content_type,
            "etag": etag,
            "created_parents": created_parents,
        }

    def _read_dify_file(self, file: Any) -> bytes:
        """Stream a Dify File object into memory while enforcing max_file_bytes."""
        preloaded = getattr(file, "_blob", None)
        if isinstance(preloaded, (bytes, bytearray)):
            self._check_size(len(preloaded))
            return bytes(preloaded)

        file_url = (getattr(file, "url", None) or "").strip()
        if not file_url:
            raise ValueError("Source Dify file object is missing a download URL.")

        try:
            with httpx.stream(
                "GET",
                file_url,
                timeout=self.timeout,
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                return read_limited(response, self.max_file_bytes)
        except httpx.UnsupportedProtocol as exc:
            raise ValueError(
                f"Invalid Dify file URL '{file_url}': {exc}. "
                "Ensure the FILES_URL environment variable is configured properly."
            ) from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Failed to download source file from Dify: {exc}") from exc

    def upload(
        self,
        path: str,
        file: Any,
        overwrite: bool = False,
        *,
        create_parents: bool = True,
        if_match_etag: str = "",
    ) -> dict[str, Any]:
        """Upload a Dify File object to WebDAV."""
        self._require_writable("Uploading files")
        raw_path = (path or "").strip()
        if (not raw_path or raw_path.endswith("/")) and getattr(file, "filename", None):
            raw_path = f"{raw_path.rstrip('/')}/{file.filename}".lstrip("/")

        rel_path = normalize_rel_path(raw_path, allow_empty=False)
        self._check_size(getattr(file, "size", None) or 0)

        data = self._read_dify_file(file)
        content_type = guess_mime_type(rel_path, getattr(file, "mime_type", None))
        return self._put_bytes(
            rel_path,
            data,
            content_type=content_type,
            overwrite=overwrite,
            create_parents=create_parents,
            if_match_etag=if_match_etag,
        )

    def read_text(
        self,
        path: str,
        *,
        encoding: str = "utf-8",
        max_chars: int = 100000,
    ) -> dict[str, Any]:
        """Download and decode a text file from WebDAV."""
        codec = (encoding or "utf-8").strip()
        try:
            b"".decode(codec)
        except LookupError as exc:
            raise ValueError(f"Unsupported text encoding: '{codec}'.") from exc

        data, meta = self.download(path)
        try:
            text = data.decode(codec)
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"File '{meta['path']}' could not be decoded using '{codec}'. "
                "It may be a binary file or use a different character encoding."
            ) from exc

        total_chars = len(text)
        truncated = False
        if max_chars > 0 and total_chars > max_chars:
            text = text[:max_chars]
            truncated = True

        return {
            "path": meta["path"],
            "content": text,
            "size": meta["size"],
            "total_chars": total_chars,
            "truncated": truncated,
            "etag": meta["etag"],
            "last_modified": meta["last_modified"],
        }

    def write_text(
        self,
        path: str,
        content: str,
        *,
        mode: str = "create",
        encoding: str = "utf-8",
        content_type: str = "",
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """Write, create, or append text content directly to a WebDAV file."""
        self._require_writable("Writing text files")
        rel_path = normalize_rel_path(path, allow_empty=False)
        write_mode = (mode or "create").strip().lower()
        if write_mode not in ("create", "overwrite", "append"):
            raise ValueError("Parameter 'mode' must be 'create', 'overwrite', or 'append'.")

        codec = (encoding or "utf-8").strip()
        new_text = "" if content is None else str(content)
        try:
            encoded_new = new_text.encode(codec)
        except LookupError as exc:
            raise ValueError(f"Unsupported text encoding: '{codec}'.") from exc
        except UnicodeEncodeError as exc:
            raise ValueError(f"Text content cannot be encoded with '{codec}': {exc}") from exc

        known_existing: dict[str, Any] | None = None
        lock_etag = ""
        if write_mode == "append":
            snapshot = self.stat(rel_path)
            if snapshot["exists"]:
                if snapshot["type"] == "directory":
                    raise ValueError(f"Target path '{rel_path}' is a directory, not a file.")
                old_bytes, old_meta = self.download(rel_path, _existing=snapshot)
                try:
                    old_text = old_bytes.decode(codec)
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"Existing file '{rel_path}' could not be decoded using '{codec}' "
                        "for text append."
                    ) from exc
                # Release intermediates eagerly: holding bytes, text, joined text and
                # payload together peaks at ~4x the file size.
                del old_bytes
                combined = old_text + new_text
                del old_text
                payload = combined.encode(codec)
                del combined
                # Leave known_existing unset so _put_bytes re-stats and detects
                # concurrent writes between the read and the PUT.
                lock_etag = snapshot["etag"] or old_meta["etag"]
                allow_overwrite = True
            else:
                payload = encoded_new
                known_existing = snapshot
                allow_overwrite = False
        else:
            payload = encoded_new
            allow_overwrite = write_mode == "overwrite"

        base_mime = (content_type or "").strip()
        if not base_mime:
            guessed = guess_mime_type(rel_path)
            if guessed == "application/octet-stream":
                guessed = "text/plain"
            base_mime = f"{guessed}; charset={codec}" if "charset=" not in guessed else guessed

        res = self._put_bytes(
            rel_path,
            payload,
            content_type=base_mime,
            overwrite=allow_overwrite,
            create_parents=create_parents,
            if_match_etag=lock_etag,
            _existing=known_existing,
        )
        return {**res, "mode": write_mode}

    def _transfer_item(
        self,
        method: str,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        create_parents: bool = True,
    ) -> dict[str, Any]:
        self._require_writable("Moving items" if method == "MOVE" else "Copying items")
        src = normalize_rel_path(source_path, allow_empty=False)
        dst = normalize_rel_path(destination_path, allow_empty=False)
        if src == dst:
            raise ValueError("Source path and destination path must be different.")

        src_info = self.stat(src)
        if not src_info["exists"]:
            raise ValueError(f"Source path '{src}' does not exist.")

        is_dir = src_info["type"] == "directory"
        if is_dir and dst.startswith(src + "/"):
            raise ValueError(
                f"Cannot {method.lower()} directory '{src}' into its own subdirectory '{dst}'."
            )

        dst_info = self.stat(dst)
        if dst_info["exists"] and not overwrite:
            raise ValueError(
                f"Destination path '{dst}' already exists and overwrite is disabled."
            )
        if dst_info["exists"] and dst_info["type"] != src_info["type"]:
            # Servers disagree here: rclone and WsgiDAV silently replace a whole
            # directory with the file, Apache answers 301 and nginx 409.
            raise ValueError(
                f"Cannot replace existing {dst_info['type']} '{dst}' with {src_info['type']} "
                f"'{src}'. Delete the destination first if this is intended."
            )

        created_parents = self._prepare_parent(dst, dst_info, create_parents)

        src_url = self.dir_url(src) if is_dir else self.item_url(src)
        dst_url = self.dir_url(dst) if is_dir else self.item_url(dst)

        headers = {
            "Destination": dst_url,
            "Overwrite": "T" if overwrite else "F",
        }
        if method == "COPY":
            headers["Depth"] = "infinity"

        data, _, status_code, _ = self.request(
            method,
            src_url,
            statuses={201, 204, 207},
            headers=headers,
            limit=MAX_XML_BYTES,
        )
        if status_code == 207:
            self._check_multistatus_errors(data, method)

        return {
            "source_path": src,
            "destination_path": dst,
            "type": src_info["type"],
            "overwritten": bool(dst_info["exists"]),
            "created_parents": created_parents,
        }

    def move(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """Move or rename a file or directory on the WebDAV server."""
        return self._transfer_item(
            "MOVE",
            source_path,
            destination_path,
            overwrite=overwrite,
            create_parents=create_parents,
        )

    def copy(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """Copy a file or directory on the WebDAV server."""
        return self._transfer_item(
            "COPY",
            source_path,
            destination_path,
            overwrite=overwrite,
            create_parents=create_parents,
        )

    def delete(self, path: str, *, missing_ok: bool = False) -> dict[str, Any]:
        """Delete a file or directory on the WebDAV server."""
        self._require_writable("Deleting items")
        rel_path = normalize_rel_path(path, allow_empty=False)
        info = self.stat(rel_path)
        if not info["exists"]:
            if missing_ok:
                return {"path": rel_path, "deleted": False, "type": ""}
            raise ValueError(f"Target path '{rel_path}' does not exist.")

        target_url = self.dir_url(rel_path) if info["type"] == "directory" else self.item_url(rel_path)
        data, _, status_code, _ = self.request(
            "DELETE",
            target_url,
            statuses={200, 202, 204, 207},
            limit=MAX_XML_BYTES,
        )
        if status_code == 207:
            self._check_multistatus_errors(data, "DELETE")

        return {
            "path": rel_path,
            "deleted": True,
            "type": info["type"],
        }

    def _prop_target(self, path: str) -> tuple[str, str, dict[str, Any]]:
        rel_path = normalize_rel_path(path, allow_empty=True)
        info = self.stat(rel_path)
        if not info["exists"]:
            raise ValueError(f"Path '{rel_path or '/'}' does not exist.")
        url = self.dir_url(rel_path) if info["type"] == "directory" else self.item_url(rel_path)
        return rel_path, url, info

    def _propfind_response(self, url: str, body: bytes) -> ElementTree.Element:
        data, _, _, _ = self.request(
            "PROPFIND",
            url,
            statuses={207},
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            content=body,
            limit=MAX_XML_BYTES,
        )
        response = self._parse_multistatus(data).find(DAV_NS + "response")
        if response is None:
            raise ValueError("Server returned no WebDAV properties.")
        return response

    def get_properties(
        self,
        path: str = "",
        names: Any = None,
        *,
        namespace: str = DEFAULT_PROP_NS,
    ) -> dict[str, Any]:
        """Read selected properties, or every non-DAV: property when no names are given."""
        namespace = (namespace or DEFAULT_PROP_NS).strip()
        requested = parse_prop_names(names, namespace, allow_dav=True)
        if len(requested) > MAX_PROPS_PER_CALL:
            raise ValueError(f"At most {MAX_PROPS_PER_CALL} properties can be read per call.")
        rel_path, url, info = self._prop_target(path)

        truncated = False
        if not requested:
            # Some servers (e.g. WsgiDAV) answer propname with no properties at all,
            # others reject it; fall back to allprop, which must include dead properties.
            for body in (PROPNAME_BODY, ALLPROP_BODY):
                try:
                    response = self._propfind_response(url, body)
                except WebDAVConnectionError:
                    raise
                except ValueError:
                    if body is ALLPROP_BODY:
                        raise
                    continue
                for tag in self._extract_props(response):
                    match = _CLARK_RE.match(tag)
                    if not match or match.group(1) == "DAV:":
                        continue
                    prop = (match.group(1), match.group(2))
                    if prop not in requested:
                        requested.append(prop)
                if requested:
                    break
            truncated = len(requested) > MAX_PROPS_PER_CALL
            requested = requested[:MAX_PROPS_PER_CALL]

        entries: list[dict[str, str]] = []
        values: dict[str, str] = {}
        missing: list[str] = []
        if requested:
            props = self._extract_props(self._propfind_response(url, _build_propfind(requested)))
            for prop in requested:
                key = prop_key(prop, namespace)
                elem = props.get(_prop_tag(prop))
                if elem is None:
                    missing.append(key)
                    continue
                value = _prop_value(elem)
                entries.append({"name": prop[1], "namespace": prop[0], "value": value})
                values[key] = value

        return {
            "path": rel_path,
            "type": info["type"],
            "namespace": namespace,
            "count": len(entries),
            "properties": entries,
            "values": values,
            "missing": missing,
            "truncated": truncated,
        }

    def set_properties(
        self,
        path: str,
        properties: Any = None,
        remove: Any = None,
        *,
        namespace: str = DEFAULT_PROP_NS,
        verify: bool = True,
    ) -> dict[str, Any]:
        """Set and/or remove custom (dead) properties via PROPPATCH, then read them back."""
        self._require_writable("Setting properties")
        namespace = (namespace or DEFAULT_PROP_NS).strip()
        to_set = parse_prop_values(properties, namespace)
        to_remove = parse_prop_names(remove, namespace, allow_dav=False)
        if not to_set and not to_remove:
            raise ValueError("Provide at least one property to set or remove.")
        overlap = [prop_key(p, namespace) for p in to_remove if p in to_set]
        if overlap:
            raise ValueError(f"Properties cannot be set and removed at once: {', '.join(overlap)}.")
        if len(to_set) + len(to_remove) > MAX_PROPS_PER_CALL:
            raise ValueError(f"At most {MAX_PROPS_PER_CALL} properties can be changed per call.")

        rel_path, url, info = self._prop_target(path)
        data, _, status_code, _ = self.request(
            "PROPPATCH",
            url,
            statuses={200, 207, 405, 501},
            headers={"Content-Type": "application/xml; charset=utf-8"},
            content=_build_propertyupdate(to_set, to_remove),
            limit=MAX_XML_BYTES,
        )
        if status_code in (405, 501):
            raise ValueError(
                f"This WebDAV server does not support custom properties (PROPPATCH returned "
                f"HTTP {status_code}; for example, nginx's WebDAV modules have no PROPPATCH)."
            )
        if status_code == 207:
            self._raise_proppatch_failures(data, namespace)

        verified = False
        if verify:
            touched = [*to_set, *to_remove]
            props = self._extract_props(self._propfind_response(url, _build_propfind(touched)))
            not_persisted = [
                prop_key(p, namespace)
                for p, value in to_set.items()
                if _prop_tag(p) not in props
                or _prop_value(props[_prop_tag(p)]).strip() != value.strip()
            ] + [prop_key(p, namespace) for p in to_remove if _prop_tag(p) in props]
            if not_persisted:
                raise ValueError(
                    "The server accepted PROPPATCH but the read-back does not match for: "
                    f"{', '.join(not_persisted)}. The server probably does not store custom "
                    "properties (for example, rclone acknowledges PROPPATCH without saving)."
                )
            verified = True

        return {
            "path": rel_path,
            "type": info["type"],
            "namespace": namespace,
            "set": [prop_key(p, namespace) for p in to_set],
            "removed": [prop_key(p, namespace) for p in to_remove],
            "verified": verified,
            "values": {prop_key(p, namespace): v for p, v in to_set.items()},
        }

    def _raise_proppatch_failures(self, data: bytes, namespace: str) -> None:
        """PROPPATCH is atomic: any failed property means nothing was applied."""
        if not data.strip():
            return
        failures: list[str] = []
        dependent: list[str] = []
        for response in self._parse_multistatus(data).findall(DAV_NS + "response"):
            for group in response.findall(DAV_NS + "propstat"):
                parts = (group.findtext(DAV_NS + "status") or "").split()
                code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
                if code < 400:
                    continue
                prop = group.find(DAV_NS + "prop")
                for node in prop if prop is not None else []:
                    match = _CLARK_RE.match(node.tag)
                    key = prop_key((match.group(1), match.group(2)), namespace) if match else node.tag
                    (dependent if code == 424 else failures).append(f"{key} (HTTP {code})")
        if failures or dependent:
            hint = (
                " HTTP 403 usually means the property is protected or custom properties "
                "are not supported."
                if any("HTTP 403" in item for item in failures)
                else ""
            )
            raise ValueError(
                "PROPPATCH was rejected and no properties were changed: "
                f"{', '.join(failures or dependent)}.{hint}"
            )
