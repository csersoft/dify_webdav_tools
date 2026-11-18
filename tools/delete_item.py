"""Tool for deleting a file or directory on WebDAV."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_str


class DeleteItem(Tool):
    """Delete a file or directory on WebDAV via HTTP DELETE."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        missing_ok = as_bool(tool_parameters.get("missing_ok"), default=False)

        with WebDAV(self.runtime.credentials) as client:
            result = client.delete(path, missing_ok=missing_ok)

        if result["deleted"]:
            summary = f"Deleted {result['type']} '{result['path']}'."
        else:
            summary = f"Target '{result['path']}' did not exist; nothing deleted."
        yield from emit_result(self, result, summary)
