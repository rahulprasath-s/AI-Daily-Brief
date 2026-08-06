import asyncio
import json


try:
    from workers import fetch as _workers_fetch
except (ImportError, ModuleNotFoundError):
    _workers_fetch = None

try:
    from js import fetch as _js_fetch
except (ImportError, ModuleNotFoundError):
    _js_fetch = None


class _PortableResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}

    async def text(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


def _standard_library_fetch(url, method="GET", headers=None, body=None, timeout=30):
    import ssl

    import certifi

    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    request_headers = {
        "User-Agent": "AI-Daily-Brief/1.0 (+https://github.com/rahulprasath-s/AI-Daily-Brief)",
        **(headers or {}),
    }
    encoded_body = body.encode("utf-8") if isinstance(body, str) else body
    request = Request(
        url,
        data=encoded_body,
        headers=request_headers,
        method=method,
    )

    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(request, timeout=timeout, context=ssl_context) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            response_body = response.read().decode(charset, errors="replace")
            return _PortableResponse(
                response.status,
                response_body,
                dict(response.headers.items()),
            )
    except HTTPError as exc:
        charset = exc.headers.get_content_charset() or "utf-8"
        response_body = exc.read().decode(charset, errors="replace")
        return _PortableResponse(
            exc.code,
            response_body,
            dict(exc.headers.items()),
        )


async def fetch(url, method="GET", headers=None, body=None, timeout=30):
    platform_fetch = _workers_fetch if callable(_workers_fetch) else _js_fetch

    if callable(platform_fetch):
        options = {}
        if method != "GET":
            options["method"] = method
        if headers is not None:
            options["headers"] = headers
        if body is not None:
            options["body"] = body
        return await platform_fetch(url, **options)

    return await asyncio.to_thread(
        _standard_library_fetch,
        url,
        method,
        headers,
        body,
        timeout,
    )


async def response_json(response):
    data = await response.json()
    return data.to_py() if hasattr(data, "to_py") else data
