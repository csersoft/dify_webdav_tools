"""Tool for reading WebDAV properties (including custom dead properties)."""

from dify_plugin import Tool

from dav_client import DEFAULT_PROP_NS, WebDAV, emit_result


class GetProperties(Tool):
    """Read selected properties, or all non-DAV: properties, of a WebDAV file or directory."""

    def _invoke(self, tool_parameters: dict):
        path = tool_parameters.get("path") or ""
        names = tool_parameters.get("names")
        namespace = tool_parameters.get("namespace") or DEFAULT_PROP_NS

        with WebDAV(self.runtime.credentials) as client:
            result = client.get_properties(path, names, namespace=namespace)

        target_label = f"'{result['path']}'" if result["path"] else "'/'"
        missing_note = f"; not found: {', '.join(result['missing'])}" if result["missing"] else ""
        summary = f"Read {result['count']} property value(s) of {target_label}{missing_note}."
        yield from emit_result(self, result, summary)
