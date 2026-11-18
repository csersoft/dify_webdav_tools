"""Tool for uploading a Dify file variable to WebDAV."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, emit_result, require_param, require_str


class UploadFile(Tool):
    """Upload an upstream Dify File object to WebDAV."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        file_obj = require_param(tool_parameters, "file")
        overwrite = as_bool(tool_parameters.get("overwrite"), default=False)
        create_parents = as_bool(tool_parameters.get("create_parents"), default=True)
        if_match_etag = tool_parameters.get("if_match_etag") or ""

        with WebDAV(self.runtime.credentials) as client:
            result = client.upload(
                path,
                file_obj,
                overwrite=overwrite,
                create_parents=create_parents,
                if_match_etag=if_match_etag,
            )

        summary = (
            f"Uploaded '{result['path']}' "
            f"({result['size']} bytes, {result['content_type']})."
        )
        yield from emit_result(self, result, summary)
