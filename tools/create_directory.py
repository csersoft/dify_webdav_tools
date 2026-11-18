"""Tool for creating directories on WebDAV via MKCOL."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_str


class CreateDirectory(Tool):
    """Create a remote WebDAV directory (optionally with parent folders)."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        parents = as_bool(tool_parameters.get("parents"), default=True)

        with WebDAV(self.runtime.credentials) as client:
            result = client.create_directory(path, parents=parents)

        if result["created"]:
            summary = f"Created directory '{result['path']}'."
        else:
            summary = f"Directory '{result['path']}' already exists."
        yield from emit_result(self, result, summary)
