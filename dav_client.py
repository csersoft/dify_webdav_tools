"""Lightweight WebDAV client built on httpx and defusedxml."""

from __future__ import annotations

import fnmatch
import mimetypes
from collections import deque
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
from defusedxml import ElementTree

DAV_NS = "{DAV:}"
MAX_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB single-file transfer limit
MAX_XML_BYTES = 8 * 1024 * 1024  # 8 MiB multistatus XML response limit
MAX_REDIRECTS = 3
MAX_SCAN_DIRS = 250

PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<propfind xmlns="DAV:"><prop>'
    b"<resourcetype/><getcontentlength/><getlastmodified/>"
    b"<getcontenttype/><getetag/>"
    b"</prop></propfind>"
)

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
    buffer = bytearray()
    for chunk in response.iter_bytes(chunk_size=65536):
        if len(buffer) + len(chunk) > limit:
            limit_mib = limit / (1024 * 1024)
            raise ValueError(f"Response body exceeds the {limit_mib:.0f} MiB limit.")
        buffer.extend(chunk)
    return bytes(buffer)


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
        self._client: httpx.Client | None = None

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
                        raise ValueError(
                            f"{method} failed with HTTP {response.status_code}. {hint}"
                        )

                    data = read_limited(response, limit) if limit > 0 else b""
                    return data, response.headers, response.status_code, target_url

            raise ValueError(f"{method} exceeded maximum redirects ({MAX_REDIRECTS}).")
        except httpx.RequestError as exc:
            raise ValueError(
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

    def stat(self, path: str = "") -> dict[str, Any]:
        """Inspect a file or directory without raising an error when not found."""
        rel_path = normalize_rel_path(path, allow_empty=True)
        url = self.item_url(rel_path) if rel_path else self.root
        name = rel_path.rsplit("/", 1)[-1] if rel_path else "/"

        data, _, status_code, _ = self.request(
            "PROPFIND",
            url,
            statuses={207, 404, 410},
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
            content=PROPFIND_BODY,
            limit=MAX_XML_BYTES,
        )

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
                return {"exists": True, **entry}

        raise ValueError(f"Server did not return WebDAV properties for '{rel_path or '/'}'.")

    def _list_single_dir(self, rel_path: str) -> list[dict[str, Any]]:
        """List immediate children of a single directory via Depth: 1 PROPFIND."""
        url = self.dir_url(rel_path)
        data, _, _, effective_url = self.request(
            "PROPFIND",
            url,
            statuses={207},
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
            content=PROPFIND_BODY,
            limit=MAX_XML_BYTES,
        )

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
        truncated = False

        queue: deque[tuple[str, int]] = deque([(rel_path, 1)])
        visited: set[str] = {rel_path}
        scanned_dirs = 0

        while queue:
            current_dir, current_depth = queue.popleft()
            scanned_dirs += 1
            if scanned_dirs > MAX_SCAN_DIRS:
                truncated = True
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
                    truncated = True
                    queue.clear()
                    break
                results.append(child)

        return {
            "path": rel_path,
            "entries": results,
            "count": len(results),
            "truncated": truncated,
        }

    def create_directory(self, path: str, *, parents: bool = True) -> dict[str, Any]:
        """Create a WebDAV directory via MKCOL, optionally creating parent folders."""
        rel_path = normalize_rel_path(path, allow_empty=False)
        existing = self.stat(rel_path)
        if existing["exists"]:
            if existing["type"] != "directory":
                raise ValueError(
                    f"Cannot create directory '{rel_path}': a file already exists at that path."
                )
            return {"path": rel_path, "created": False, "already_exists": True}

        allowed = {201, 409} if parents else {201}
        _, _, status_code, _ = self.request("MKCOL", self.dir_url(rel_path), statuses=allowed)
        if status_code == 201:
            return {"path": rel_path, "created": True, "already_exists": False}

        # Parent folder missing (HTTP 409) and parents=True: create hierarchy top-down.
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
            if code == 405:
                recheck = self.stat(current)
                if not (recheck["exists"] and recheck["type"] == "directory"):
                    raise ValueError(f"Failed to create directory '{current}' (HTTP 405).")

        return {"path": rel_path, "created": True, "already_exists": False}

    def _ensure_parent_dir(self, rel_path: str) -> bool:
        if "/" not in rel_path:
            return False
        parent_dir = rel_path.rsplit("/", 1)[0]
        res = self.create_directory(parent_dir, parents=True)
        return bool(res["created"])

    def download(
        self,
        path: str,
        *,
        _existing: dict[str, Any] | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """Download a WebDAV file up to MAX_FILE_BYTES and return bytes + metadata."""
        rel_path = normalize_rel_path(path, allow_empty=False)
        info = _existing if _existing is not None else self.stat(rel_path)
        if not info["exists"]:
            raise ValueError(f"File '{rel_path}' does not exist.")
        if info["type"] == "directory":
            raise ValueError(f"Path '{rel_path}' is a directory, not a file.")

        data, headers, _, _ = self.request(
            "GET",
            self.item_url(rel_path),
            statuses={200},
            limit=MAX_FILE_BYTES,
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
        if len(data) > MAX_FILE_BYTES:
            limit_mib = MAX_FILE_BYTES // (1024 * 1024)
            raise ValueError(f"File size exceeds the {limit_mib} MiB single-file limit.")

        existing = _existing if _existing is not None else self.stat(rel_path)
        if existing["exists"] and existing["type"] == "directory":
            raise ValueError(f"Target path '{rel_path}' is an existing directory, not a file.")

        expected_etag = if_match_etag.strip()
        if expected_etag:
            if not existing["exists"]:
                raise ValueError(
                    f"Precondition failed: target '{rel_path}' does not exist for If-Match."
                )
            if existing["etag"] and existing["etag"] != expected_etag:
                raise ValueError(
                    f"ETag mismatch for '{rel_path}': remote ETag is {existing['etag']}."
                )
        elif not overwrite and existing["exists"]:
            raise ValueError(
                f"Target file '{rel_path}' already exists and overwrite is disabled."
            )

        created_parents = (
            self._ensure_parent_dir(rel_path)
            if (create_parents and not existing["exists"])
            else False
        )

        headers = {"Content-Type": content_type}
        if expected_etag:
            headers["If-Match"] = expected_etag
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
        return {
            "path": rel_path,
            "size": len(data),
            "content_type": content_type,
            "etag": etag,
            "created_parents": created_parents,
        }

    def _read_dify_file(self, file: Any) -> bytes:
        """Stream a Dify File object into memory while enforcing MAX_FILE_BYTES."""
        preloaded = getattr(file, "_blob", None)
        if isinstance(preloaded, (bytes, bytearray)):
            if len(preloaded) > MAX_FILE_BYTES:
                limit_mib = MAX_FILE_BYTES // (1024 * 1024)
                raise ValueError(f"File size exceeds the {limit_mib} MiB single-file limit.")
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
                return read_limited(response, MAX_FILE_BYTES)
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
        raw_path = (path or "").strip()
        if (not raw_path or raw_path.endswith("/")) and getattr(file, "filename", None):
            raw_path = f"{raw_path.rstrip('/')}/{file.filename}".lstrip("/")

        rel_path = normalize_rel_path(raw_path, allow_empty=False)
        declared_size = getattr(file, "size", None) or 0
        if declared_size > MAX_FILE_BYTES:
            limit_mib = MAX_FILE_BYTES // (1024 * 1024)
            raise ValueError(f"File size exceeds the {limit_mib} MiB single-file limit.")

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
        mode: str = "overwrite",
        encoding: str = "utf-8",
        content_type: str = "",
        create_parents: bool = True,
    ) -> dict[str, Any]:
        """Write, create, or append text content directly to a WebDAV file."""
        rel_path = normalize_rel_path(path, allow_empty=False)
        write_mode = (mode or "overwrite").strip().lower()
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
        if write_mode == "append":
            known_existing = self.stat(rel_path)
            if known_existing["exists"]:
                if known_existing["type"] == "directory":
                    raise ValueError(f"Target path '{rel_path}' is a directory, not a file.")
                old_bytes, _ = self.download(rel_path, _existing=known_existing)
                try:
                    old_text = old_bytes.decode(codec)
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        f"Existing file '{rel_path}' could not be decoded using '{codec}' "
                        "for text append."
                    ) from exc
                payload = (old_text + new_text).encode(codec)
            else:
                payload = encoded_new
            allow_overwrite = True
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

        created_parents = (
            self._ensure_parent_dir(dst)
            if (create_parents and not dst_info["exists"])
            else False
        )

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
