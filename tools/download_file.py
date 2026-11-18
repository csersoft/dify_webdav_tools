"""Tool for downloading a WebDAV file as a Dify blob/file variable."""

from dify_plugin import Tool

from dav_client import WebDAV, emit_result, require_str


class DownloadFile(Tool):
    """Download a remote WebDAV file and emit both Dify blob and metadata."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        with WebDAV(self.runtime.credentials) as client:
            data, meta = client.download(path)

        yield self.create_blob_message(
            data,
            {
                "mime_type": meta["mime_type"],
                "filename": meta["filename"],
            },
        )
        summary = (
            f"Downloaded '{meta['path']}' "
            f"({meta['size']} bytes, {meta['mime_type']})."
        )
        yield from emit_result(self, meta, summary)
