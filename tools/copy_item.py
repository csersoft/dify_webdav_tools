"""Tool for copying a file or directory directly on WebDAV."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_str


class CopyItem(Tool):
    """Copy a WebDAV resource server-side via HTTP COPY."""

    def _invoke(self, tool_parameters: dict):
        source_path = require_str(tool_parameters, "source_path")
        destination_path = require_str(tool_parameters, "destination_path")
        overwrite = as_bool(tool_parameters.get("overwrite"), default=False)
        create_parents = as_bool(tool_parameters.get("create_parents"), default=True)

        with WebDAV(self.runtime.credentials) as client:
            result = client.copy(
                source_path,
                destination_path,
                overwrite=overwrite,
                create_parents=create_parents,
            )

        summary = (
            f"Copied {result['type']} '{result['source_path']}' "
            f"to '{result['destination_path']}'."
        )
        yield from emit_result(self, result, summary)
