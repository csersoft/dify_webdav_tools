"""Tool for writing or appending string content directly to a WebDAV file."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_str


class WriteTextFile(Tool):
    """Encode and write text content directly to WebDAV."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        raw_content = tool_parameters.get("content")
        content = "" if raw_content is None else str(raw_content)
        mode = tool_parameters.get("mode") or "create"
        encoding = tool_parameters.get("encoding") or "utf-8"
        content_type = tool_parameters.get("content_type") or ""
        create_parents = as_bool(tool_parameters.get("create_parents"), default=True)

        with WebDAV(self.runtime.credentials) as client:
            result = client.write_text(
                path,
                content,
                mode=mode,
                encoding=encoding,
                content_type=content_type,
                create_parents=create_parents,
            )

        summary = (
            f"Wrote '{result['path']}' in '{result['mode']}' mode "
            f"(total size: {result['size']} bytes)."
        )
        yield from emit_result(self, result, summary)
