"""MCP resources: files below ``NC_MCP_RESOURCE_ROOT`` as ``resources/list`` and ``resources/read``.

Off until the variable is set: then the list is empty, no template is advertised and every
read is refused. Sandbox, ``kein-ki`` guard and size check are those of the file tools; the
URI is the one ``files_download`` embeds, ``nextcloud://files/Docs/budget.xlsx``. It is a
server subclass because the SDK list handler has no request context for the credentials and
a template function has one fixed MIME type for all files.
"""

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.mcpserver.resources.templates import ResourceSecurity
from mcp.shared.exceptions import MCPError
from mcp.shared.uri_template import UriTemplate
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ListResourcesResult,
    Resource,
    ResourceTemplate,
)

from .. import config, deps
from ..errors import ToolError
from ..tools import files as files_tools

__all__ = ["URI_PREFIX", "ResourceServer", "file_uri"]

URI_PREFIX = "nextcloud://files"
URI_TEMPLATE = f"{URI_PREFIX}/{{+path}}"
_TEMPLATE = UriTemplate.parse(URI_TEMPLATE)
_SECURITY = ResourceSecurity()


def file_uri(path: str) -> str:
    """The resource URI of an absolute Nextcloud path."""
    return f"{URI_PREFIX}{quote(path, safe='/')}"


def _mcp_error(exc: ToolError | httpx.HTTPError) -> MCPError:
    if isinstance(exc, ToolError):
        return MCPError(code=INVALID_PARAMS, message=f"{exc.message} Hint: {exc.hint}")
    what = (
        "did not respond in time" if isinstance(exc, httpx.TimeoutException) else "is unreachable"
    )
    return MCPError(code=INTERNAL_ERROR, message=f"Nextcloud {what}. Hint: retry later.")


class ResourceServer(MCPServer):
    """``MCPServer`` that serves the files below ``NC_MCP_RESOURCE_ROOT`` as resources."""

    async def _handle_list_resources(self, ctx: Any, params: Any) -> ListResourcesResult:
        root = config.resource_root()
        if root is None:
            return await super()._handle_list_resources(ctx, params)
        context = Context(
            request_context=ctx,
            mcp_server=self,
            input_params=params,
            subscriptions=self._subscriptions,
        )
        try:
            files = await files_tools.list_resources(
                deps.resolve_clients(context), root, config.resource_max_bytes()
            )
        except (ToolError, httpx.HTTPError) as exc:
            raise _mcp_error(exc) from None
        return ListResourcesResult(
            resources=[
                Resource(
                    uri=file_uri(item["path"]),
                    name=item["name"],
                    mime_type=files_tools.resource_content_type(item),
                    size=item["size"],
                )
                for item in files
            ]
        )

    async def list_resource_templates(self) -> list[ResourceTemplate]:
        templates = await super().list_resource_templates()
        if config.resource_root() is None:
            return templates
        return [
            *templates,
            ResourceTemplate(
                uri_template=URI_TEMPLATE,
                name="nextcloud_file",
                description=(
                    "A file from the user's Nextcloud, read as a base64 blob. "
                    "Files above the size limit are refused; use files_download for those."
                ),
            ),
        ]

    async def read_resource(
        self, uri: Any, context: Context[Any, Any] | None = None
    ) -> Iterable[ReadResourceContents] | Any:
        if config.resource_root() is None or not str(uri).startswith(f"{URI_PREFIX}/"):
            return await super().read_resource(uri, context)
        params = _TEMPLATE.match(str(uri))
        if params is None or _SECURITY.validate(params) is not None:
            raise ResourceNotFoundError(f"Unknown resource: {uri}")
        try:
            result = await files_tools.read_resource(
                deps.resolve_clients(context), f"/{params['path']}", config.resource_max_bytes()
            )
        except (ToolError, httpx.HTTPError) as exc:
            raise _mcp_error(exc) from None
        return [ReadResourceContents(content=result["content"], mime_type=result["content_type"])]
