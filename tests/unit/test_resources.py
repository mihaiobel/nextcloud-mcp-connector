"""Tests for the MCP resources: tool logic with respx, then the in-memory MCP client."""

import base64

import guard_routes
import httpx
import pytest
import respx
from mcp import Client
from mcp.shared.exceptions import MCPError

from mcp_connector import config
from mcp_connector.errors import ToolError
from mcp_connector.nextcloud import NcClients
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.server import mcp
from mcp_connector.server.resources import file_uri
from mcp_connector.tools import files as files_tools

BASE = "http://nc.test"
USER = "alice"
HOME = f"{BASE}/remote.php/dav/files/{USER}"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PAYLOAD = bytes(range(256)) * 4
OCTET = "application/octet-stream"
PDF = "application/pdf"


@pytest.fixture(autouse=True)
def _no_kein_ki_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    guard_routes.patch_untagged(monkeypatch)


@pytest.fixture
def stdio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_URL, BASE)
    monkeypatch.setenv(config.ENV_USER, USER)
    monkeypatch.setenv(config.ENV_APP_PASSWORD, "app-password-test")
    monkeypatch.setenv(config.ENV_RESOURCE_ROOT, "/Share")


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False), creds=Credentials(BASE, USER, "x")
    )


def _entry(path: str, size: int | None = None, ctype: str = "") -> str:
    """One DAV response; ``size=None`` makes it a folder."""
    kind = "<d:collection/>" if size is None else ""
    length = "" if size is None else f"<d:getcontentlength>{size}</d:getcontentlength>"
    mime = f"<d:getcontenttype>{ctype}</d:getcontenttype>" if ctype else ""
    return (
        f"<d:response><d:href>/remote.php/dav/files/{USER}{path}</d:href><d:propstat><d:prop>"
        f"<d:resourcetype>{kind}</d:resourcetype>{length}{mime}<oc:fileid>7</oc:fileid>"
        "<oc:permissions>RGDNVW</oc:permissions></d:prop><d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat></d:response>"
    )


def _propfind(mock: respx.MockRouter, path: str, *entries: str) -> None:
    xml = '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">' + "".join(entries)
    mock.route(method="PROPFIND", url=f"{HOME}{path}").mock(
        return_value=httpx.Response(207, text=xml + "</d:multistatus>")
    )


def _mock_share(mock: respx.MockRouter) -> None:
    _propfind(
        mock,
        "/Share",
        _entry("/Share/"),
        _entry("/Share/sub/"),
        _entry("/Share/report.xlsx", len(PAYLOAD), XLSX),
        _entry("/Share/notes.txt", 5, "text/plain"),
        _entry("/Share/huge.bin", 50_000_000, OCTET),
        _entry("/Share/raw", 3),
    )
    _propfind(mock, "/Share/sub", _entry("/Share/sub/"), _entry("/Share/sub/scan.pdf", 9, PDF))


def _mock_file(mock: respx.MockRouter, path: str, data: bytes, ctype: str) -> respx.Route:
    _propfind(mock, path, _entry(path, len(data), ctype))
    return mock.route(method="GET", url=f"{HOME}{path}").mock(
        return_value=httpx.Response(200, content=data)
    )


def test_config_resource_root_and_limit() -> None:
    assert config.resource_root({}) is None
    assert config.resource_root({config.ENV_RESOURCE_ROOT: " "}) is None
    assert config.resource_max_bytes({}) == 5 * 1024 * 1024
    for bad in ("0", "-1", "5MB"):
        with pytest.raises(ToolError):
            config.resource_max_bytes({config.ENV_RESOURCE_MAX_BYTES: bad})


@pytest.mark.anyio
async def test_list_skips_files_above_the_limit_and_respects_the_depth(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    with respx.mock as mock:
        _mock_share(mock)
        files = await files_tools.list_resources(clients, "/Share", 1_000_000)
        monkeypatch.setattr(files_tools, "RESOURCE_LIST_DEPTH", 0)
        shallow = await files_tools.list_resources(clients, "/Share", 1_000_000)

    paths = sorted(item["path"] for item in files)
    assert paths == ["/Share/notes.txt", "/Share/raw", "/Share/report.xlsx", "/Share/sub/scan.pdf"]
    assert "/Share/sub/scan.pdf" not in [item["path"] for item in shallow]


@pytest.mark.anyio
async def test_read_guesses_the_mimetype_when_nextcloud_has_none(clients: NcClients) -> None:
    with respx.mock as mock:
        _mock_file(mock, "/Share/scan.pdf", b"%PDF-", OCTET)
        result = await files_tools.read_resource(clients, "/Share/scan.pdf", 5_000_000)
    assert result["content_type"] == "application/pdf"


@pytest.mark.anyio
async def test_read_refuses_a_large_file_without_a_get(clients: NcClients) -> None:
    with respx.mock as mock:
        get = _mock_file(mock, "/Share/huge.bin", b"x" * 20, OCTET)
        with pytest.raises(ToolError) as caught:
            await files_tools.read_resource(clients, "/Share/huge.bin", 10)

    assert not get.called
    assert "files_download" in caught.value.hint


@pytest.mark.anyio
async def test_read_treats_a_kein_ki_file_like_a_missing_one(
    clients: NcClients, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    guard_routes.reset()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        guard_routes.active(mock, ("Share/geheim.txt", "7", False))
        get = _mock_file(mock, "/Share/geheim.txt", b"abc", "text/plain")
        with pytest.raises(ToolError, match="not found"):
            await files_tools.read_resource(clients, "/Share/geheim.txt", 1000)

    assert not get.called


@pytest.mark.anyio
@pytest.mark.usefixtures("stdio_env")
async def test_list_and_template_over_the_protocol() -> None:
    with respx.mock as mock:
        _mock_share(mock)
        async with Client(mcp, raise_exceptions=False) as client:
            listed = await client.list_resources()
            templates = await client.list_resource_templates()

    by_name = {item.name: item for item in listed.resources}
    assert set(by_name) == {"notes.txt", "raw", "report.xlsx", "scan.pdf"}
    assert str(by_name["report.xlsx"].uri) == "nextcloud://files/Share/report.xlsx"
    assert by_name["report.xlsx"].mime_type == XLSX
    assert by_name["raw"].mime_type == OCTET
    assert [t.uri_template for t in templates.resource_templates] == ["nextcloud://files/{+path}"]


@pytest.mark.anyio
async def test_without_a_root_nothing_is_listed_or_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ENV_RESOURCE_ROOT, raising=False)
    monkeypatch.setenv(config.ENV_URL, BASE)
    async with Client(mcp, raise_exceptions=False) as client:
        assert client.server_capabilities.resources is not None
        assert (await client.list_resources()).resources == []
        assert (await client.list_resource_templates()).resource_templates == []
        with pytest.raises(MCPError):
            await client.read_resource("nextcloud://files/Share/notes.txt")


@pytest.mark.anyio
@pytest.mark.usefixtures("stdio_env")
@pytest.mark.parametrize(
    ("files_root", "uri", "path"),
    [
        (None, "nextcloud://files/Share/report.xlsx", "/Share/report.xlsx"),
        ("/Share", "nextcloud://files/report.xlsx", "/Share/report.xlsx"),
    ],
)
async def test_read_returns_a_base64_blob_without_data_prefix(
    monkeypatch: pytest.MonkeyPatch, files_root: str | None, uri: str, path: str
) -> None:
    if files_root:
        monkeypatch.setenv(config.ENV_FILES_ROOT, files_root)
    with respx.mock(assert_all_called=True) as mock:
        _mock_file(mock, path, PAYLOAD, XLSX)
        async with Client(mcp, raise_exceptions=False) as client:
            result = await client.read_resource(uri)

    (content,) = result.contents
    blob = content.blob  # type: ignore[union-attr]
    assert content.mime_type == XLSX
    assert not blob.startswith("data:")
    assert base64.b64decode(blob, validate=True) == PAYLOAD


@pytest.mark.anyio
@pytest.mark.usefixtures("stdio_env")
@pytest.mark.parametrize(
    "uri",
    [
        "nextcloud://files/Share/../../secret.txt",
        "nextcloud://files/%2e%2e/secret.txt",
        "nextcloud://files/..%2Fsecret.txt",
        "nextcloud://files//etc/passwd",
        "nextcloud://files/Share/a%00b.txt",
    ],
)
async def test_path_traversal_is_rejected_before_any_request(uri: str) -> None:
    with respx.mock as mock:
        route = mock.route().mock(return_value=httpx.Response(500))
        async with Client(mcp, raise_exceptions=False) as client:
            with pytest.raises(MCPError):
                await client.read_resource(uri)
    assert not route.called


def test_file_uri_quotes_special_characters() -> None:
    assert file_uri("/Docs/Q3 report #1.xlsx") == "nextcloud://files/Docs/Q3%20report%20%231.xlsx"
