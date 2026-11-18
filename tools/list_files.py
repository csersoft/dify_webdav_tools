"""Tool for listing WebDAV directories with optional recursion and filtering."""

from dify_plugin import Tool

from dav_client import WebDAV, as_bool, as_int, emit_result


class ListFiles(Tool):
    """List directory entries from the configured WebDAV server."""

    def _invoke(self, tool_parameters: dict):
        path = tool_parameters.get("path") or ""
        recursive = as_bool(tool_parameters.get("recursive"), default=False)
        max_depth = as_int(
            tool_parameters.get("max_depth"),
            default=3,
            minimum=1,
            maximum=10,
            name="max_depth",
        )
        pattern = tool_parameters.get("pattern") or ""
        entry_type = tool_parameters.get("entry_type") or "all"
        limit = as_int(
            tool_parameters.get("limit"),
            default=200,
            minimum=1,
            maximum=2000,
            name="limit",
        )

        with WebDAV(self.runtime.credentials) as client:
            result = client.list_directory(
                path,
                recursive=recursive,
                max_depth=max_depth,
                pattern=pattern,
                entry_type=entry_type,
                limit=limit,
            )

        target_label = f"'{result['path']}'" if result["path"] else "'/'"
        trunc_note = (
            f" (truncated: {result['truncated_reason']})" if result["truncated"] else ""
        )
        summary = f"Listed {result['count']} item(s) under {target_label}{trunc_note}."
        yield from emit_result(self, result, summary)
