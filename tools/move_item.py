"""Tool for moving or renaming a file or directory on WebDAV."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_str


class MoveItem(Tool):
    """Move or rename a WebDAV resource via HTTP MOVE."""

    def _invoke(self, tool_parameters: dict):
        source_path = require_str(tool_parameters, "source_path")
        destination_path = require_str(tool_parameters, "destination_path")
        overwrite = as_bool(tool_parameters.get("overwrite"), default=False)
        create_parents = as_bool(tool_parameters.get("create_parents"), default=True)

        with WebDAV(self.runtime.credentials) as client:
            result = client.move(
                source_path,
                destination_path,
                overwrite=overwrite,
                create_parents=create_parents,
            )

        summary = (
            f"Moved {result['type']} '{result['source_path']}' "
            f"to '{result['destination_path']}'."
        )
        yield from emit_result(self, result, summary)
