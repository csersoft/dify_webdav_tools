"""Tool for checking existence and metadata of a WebDAV resource."""

from dify_plugin import Tool

from dav_client import WebDAV, emit_result


class StatItem(Tool):
    """Retrieve WebDAV resource metadata via Depth: 0 PROPFIND."""

    def _invoke(self, tool_parameters: dict):
        path = tool_parameters.get("path") or ""
        with WebDAV(self.runtime.credentials) as client:
            result = client.stat(path, resolve_etag=True)

        target_label = f"'{result['path']}'" if result["path"] else "'/'"
        if result["exists"]:
            summary = (
                f"Found {result['type']} at {target_label} "
                f"(size: {result['size']} bytes)."
            )
        else:
            summary = f"No file or directory exists at {target_label}."
        yield from emit_result(self, result, summary)
