#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qynet —— 用标准库实现的 `requests` 子集（零第三方依赖）。

为什么要这个
------------
`qingyi_executor.py` 里 1838 行知乎逻辑只用到 `requests` 的很小一部分：
    Session() / .headers / .get / .post / .put / .patch
    Response: .status_code / .headers / .text / .content / .json()
把这一小块用 urllib 实现出来，就能让整个执行器在**没有装任何第三方包**的
机器上跑起来。这是「用户不用装依赖」这条要求唯一的实现路径。

用法
----
在 import 执行器之前把它顶替掉即可：

    import qynet
    qynet.install()          # sys.modules["requests"] = qynet
    import qingyi_executor   # 它 import requests 时拿到的是本模块

设计取舍
--------
* 不做 cookie jar 的完整语义，只做「手动 Cookie 头 + 吸收 Set-Cookie」，
  这与 requests.Session 在本项目里的实际行为一致（代码自己拼 Cookie 头）。
* HTTP 状态码 >= 400 **不抛异常**，照常返回 Response —— 因为调用方
  全都是靠 `r.status_code != 200` 判断的，抛异常反而会改变行为。
* 证书校验失败时降级重试一次并打一次警告：python.org 版 Python 在 macOS 上
  常缺根证书，若直接失败，用户会以为「程序坏了」。
"""
from __future__ import annotations

import gzip
import json as _json
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, Mapping, Optional, Tuple

__version__ = "1.0.0"

DEFAULT_TIMEOUT = 30.0
_USER_AGENT = "qynet/1.0 (+stdlib urllib)"


# --------------------------------------------------------------------------- #
# 异常层级（对齐 requests.exceptions 的常用名字）
# --------------------------------------------------------------------------- #

class RequestException(IOError):
    """所有本模块异常的基类。"""


class ConnectionError(RequestException):  # noqa: A001 - 刻意与内建同名以对齐 requests
    pass


class Timeout(RequestException):  # noqa: A001
    pass


class TooManyRedirects(RequestException):
    pass


class HTTPError(RequestException):
    def __init__(self, msg: str, response: "Optional[Response]" = None) -> None:
        super().__init__(msg)
        self.response = response


class _Exceptions:
    """`requests.exceptions.Xxx` 的替身。"""

    RequestException = RequestException
    ConnectionError = ConnectionError
    Timeout = Timeout
    TooManyRedirects = TooManyRedirects
    HTTPError = HTTPError


exceptions = _Exceptions()


# --------------------------------------------------------------------------- #
# 大小写不敏感的响应头
# --------------------------------------------------------------------------- #

class Headers(dict):
    """dict 子类：键大小写不敏感，但保留原始大小写。"""

    def __init__(self, src: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._lower: Dict[str, str] = {}
        if src:
            for k, v in src.items():
                self[k] = v

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self._lower[key.lower()] = key

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(self._lower.get(key.lower(), key))

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        real = self._lower.get(key.lower())
        return super().get(real, default) if real else default

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._lower

    def pop(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        real = self._lower.pop(key.lower(), None)
        return super().pop(real, default) if real else default


# --------------------------------------------------------------------------- #
# 响应
# --------------------------------------------------------------------------- #

class Response:
    def __init__(self, status_code: int, headers: Headers, body: bytes,
                 url: str, reason: str = "") -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.url = url
        self.reason = reason
        self.encoding = "utf-8"
        self.elapsed = 0.0

    # ---- 内容 ---- #
    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode(self.encoding, "replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self, **kw: Any) -> Any:
        return _json.loads(self.text, **kw)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"{self.status_code} {self.reason} for url: {self.url}", self)

    def iter_lines(self, decode_unicode: bool = False):  # 给 SSE 用
        for raw in self._body.splitlines():
            yield raw.decode("utf-8", "replace") if decode_unicode else raw

    def __repr__(self) -> str:
        return f"<Response [{self.status_code}]>"

    def __bool__(self) -> bool:
        return self.ok


# --------------------------------------------------------------------------- #
# 请求体编码
# --------------------------------------------------------------------------- #

def _encode_body(json: Any, data: Any, headers: Dict[str, str]) -> Optional[bytes]:
    if json is not None:
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
        return _json.dumps(json, ensure_ascii=False).encode("utf-8")
    if data is None:
        return None
    if isinstance(data, bytes):
        return data
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, Mapping):
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return urllib.parse.urlencode(data, doseq=True).encode("utf-8")
    return str(data).encode("utf-8")


def _decode_body(raw: bytes, enc: str) -> bytes:
    e = (enc or "").lower()
    try:
        if "gzip" in e:
            return gzip.decompress(raw)
        if "deflate" in e:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - 解压失败就交回原始字节，让上层报 JSON 错
        return raw
    return raw


def _absorb_cookies(jar: Dict[str, str], headers: Headers) -> None:
    raw = headers.get("Set-Cookie")
    if not raw:
        return
    for part in str(raw).split(","):
        seg = part.split(";")[0].strip()
        if "=" in seg:
            k, v = seg.split("=", 1)
            jar[k.strip()] = v.strip()


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #

class Session:
    """与 requests.Session 的可用子集行为一致。"""

    def __init__(self) -> None:
        self.headers: Dict[str, str] = {"User-Agent": _USER_AGENT}
        self._jar: Dict[str, str] = {}
        self._ctx_verified = ssl.create_default_context()
        self._ctx_loose: Optional[ssl.SSLContext] = None
        self._warned_cert = False
        self.verify = True
        self.max_redirects = 10

    # ---- 上下文管理 ---- #
    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        pass

    # ---- 内部 ---- #
    def _cookie_header(self, explicit: Dict[str, str]) -> str:
        """手动 Cookie 头优先；jar 里多出来的补在后头。"""
        merged: Dict[str, str] = {}
        manual = next((v for k, v in explicit.items() if k.lower() == "cookie"), "")
        if manual:
            for part in manual.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    merged[k.strip()] = v.strip()
        for k, v in self._jar.items():
            merged.setdefault(k, v)
        return "; ".join(f"{k}={v}" for k, v in merged.items())

    def _build(self, method: str, url: str, params: Any, json: Any, data: Any,
               headers: Optional[Mapping[str, str]]) -> urllib.request.Request:
        if params:
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params, doseq=True)}"

        h: Dict[str, str] = dict(self.headers)
        if headers:
            h.update({k: v for k, v in headers.items()})
        h.setdefault("Accept-Encoding", "identity")   # 不让服务端压缩，省一层解压
        ck = self._cookie_header(h)
        if ck:
            h["Cookie"] = ck
        else:
            h.pop("Cookie", None)

        body = _encode_body(json, data, h)
        req = urllib.request.Request(url, data=body, method=method.upper())
        for k, v in h.items():
            req.add_header(k, v)
        return req

    def request(self, method: str, url: str, params: Any = None, json: Any = None,
                data: Any = None, headers: Optional[Mapping[str, str]] = None,
                timeout: Optional[float] = None,
                allow_redirects: bool = True) -> Response:
        req = self._build(method, url, params, json, data, headers)
        to = DEFAULT_TIMEOUT if timeout is None else float(timeout)

        for attempt in (0, 1):
            ctx = self._ctx_verified if (self.verify and attempt == 0) \
                else (self._ctx_loose or ssl._create_unverified_context())  # noqa: SLF001
            if attempt == 1:
                self._ctx_loose = ctx
            try:
                with urllib.request.urlopen(req, timeout=to, context=ctx) as resp:
                    body = _decode_body(resp.read(),
                                        resp.headers.get("Content-Encoding", ""))
                    hdrs = Headers({k: v for k, v in resp.headers.items()})
                    _absorb_cookies(self._jar, hdrs)
                    return Response(resp.status, hdrs, body, resp.geturl(),
                                    getattr(resp, "reason", "") or "")
            except urllib.error.HTTPError as e:
                body = _decode_body(e.read(), e.headers.get("Content-Encoding", ""))
                hdrs = Headers({k: v for k, v in (e.headers or {}).items()})
                _absorb_cookies(self._jar, hdrs)
                return Response(e.code, hdrs, body, url,
                                getattr(e, "reason", "") or "")
            except ssl.SSLCertVerificationError as e:
                if attempt == 0 and self.verify:
                    if not self._warned_cert:
                        self._warned_cert = True
                        print(f"[qynet] 本机缺少根证书（{e.reason if hasattr(e,'reason') else e}），"
                              f"已降级为不校验证书继续访问。")
                    continue
                raise ConnectionError(f"TLS 校验失败：{e}") from e
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, socket.timeout):
                    raise Timeout(f"请求超时（{to}s）：{url}") from e
                if isinstance(reason, ssl.SSLCertVerificationError):
                    if attempt == 0 and self.verify:
                        continue
                raise ConnectionError(f"网络错误：{reason}") from e
            except socket.timeout as e:
                raise Timeout(f"请求超时（{to}s）：{url}") from e
        raise ConnectionError(f"请求失败：{url}")

    # ---- 动词 ---- #
    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> Response:
        return self.request("PUT", url, **kw)

    def patch(self, url: str, **kw: Any) -> Response:
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw: Any) -> Response:
        return self.request("DELETE", url, **kw)

    def head(self, url: str, **kw: Any) -> Response:
        return self.request("HEAD", url, **kw)


# --------------------------------------------------------------------------- #
# 模块级便捷函数
# --------------------------------------------------------------------------- #

_GLOBAL = Session()


def request(method: str, url: str, **kw: Any) -> Response:
    return _GLOBAL.request(method, url, **kw)


def get(url: str, **kw: Any) -> Response:
    return _GLOBAL.get(url, **kw)


def post(url: str, **kw: Any) -> Response:
    return _GLOBAL.post(url, **kw)


def put(url: str, **kw: Any) -> Response:
    return _GLOBAL.put(url, **kw)


def patch(url: str, **kw: Any) -> Response:
    return _GLOBAL.patch(url, **kw)


def delete(url: str, **kw: Any) -> Response:
    return _GLOBAL.delete(url, **kw)


# --------------------------------------------------------------------------- #
# 顶替真 requests
# --------------------------------------------------------------------------- #

def install(force: bool = True) -> bool:
    """把本模块注册成 `requests`。返回是否真的接管了。

    force=True 时无条件顶替 —— 我们要的就是「行为确定、不依赖环境」，
    而不是「装了 requests 就用 requests」。
    """
    cur = sys.modules.get("requests")
    if cur is not None and not force and getattr(cur, "__qynet__", False) is False:
        return False
    sys.modules["requests"] = sys.modules[__name__]
    return True


__qynet__ = True
