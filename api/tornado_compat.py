#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, MutableMapping

import tornado.httpserver
import tornado.ioloop
import tornado.httputil
import tornado.web
from jinja2 import Template


_current_handler: contextvars.ContextVar[tornado.web.RequestHandler | None] = contextvars.ContextVar(
    "ragflow_current_handler", default=None
)
_current_app: contextvars.ContextVar["Quart | None"] = contextvars.ContextVar(
    "ragflow_current_app", default=None
)
_current_g: contextvars.ContextVar[SimpleNamespace | None] = contextvars.ContextVar(
    "ragflow_current_g", default=None
)


class Unauthorized(Exception):
    code = 401


class AuthUser:
    pass


class Request:
    pass


class URLMap:
    def __init__(self) -> None:
        self.strict_slashes = True


class CLI:
    def add_command(self, _cmd: Callable[..., Any]) -> None:
        return None


class Headers(MutableMapping[str, str]):
    def __init__(self) -> None:
        self._headers: dict[str, str] = {}

    def add_header(self, key: str, value: str) -> None:
        self._headers[key] = value

    def set(self, key: str, value: str) -> None:
        self._headers[key] = value

    def __getitem__(self, key: str) -> str:
        return self._headers[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._headers[key] = value

    def __delitem__(self, key: str) -> None:
        del self._headers[key]

    def __iter__(self):
        return iter(self._headers)

    def __len__(self) -> int:
        return len(self._headers)

    def items(self):
        return self._headers.items()

    def __contains__(self, key: object) -> bool:
        return key in self._headers


@dataclass
class Response:
    body: Any
    status_code: int = 200
    mimetype: str | None = None
    content_type: str | None = None
    headers: Headers = field(default_factory=Headers)


def _ensure_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


def jsonify(data: Any) -> Response:
    app = _current_app.get()
    encoder = None
    if app is not None:
        encoder = app.json_encoder
    body = json.dumps(data, cls=encoder) if encoder else json.dumps(data)
    resp = Response(body=body, mimetype="application/json")
    return resp


async def make_response(data: Any, status: int = 200) -> Response:
    if isinstance(data, Response):
        data.status_code = status
        return data
    resp = Response(body=data, status_code=status)
    return resp


async def send_file(
    file_obj: Any,
    as_attachment: bool = False,
    attachment_filename: str | None = None,
    mimetype: str | None = None,
) -> Response:
    if hasattr(file_obj, "read"):
        body = file_obj.read()
    else:
        with open(file_obj, "rb") as f:
            body = f.read()
    resp = Response(body=body, mimetype=mimetype or "application/octet-stream")
    if as_attachment:
        filename = attachment_filename or os.path.basename(getattr(file_obj, "name", "download"))
        resp.headers.add_header("Content-Disposition", f'attachment; filename="{filename}"')
    return resp


def redirect(location: str, code: int = 302) -> Response:
    resp = Response(body=b"", status_code=code)
    resp.headers.add_header("Location", location)
    return resp


async def render_template_string(template_str: str, **context: Any) -> str:
    template = Template(template_str or "")
    return template.render(**context)


class MultiDict:
    def __init__(self, data: Mapping[str, Iterable[Any]] | None = None) -> None:
        self._data: dict[str, list[Any]] = {}
        if data:
            for key, values in data.items():
                self._data[key] = list(values)

    def get(self, key: str, default: Any = None, type: Callable[[Any], Any] | None = None) -> Any:
        if key not in self._data or not self._data[key]:
            return default
        value = self._data[key][0]
        if type is None:
            return value
        try:
            return type(value)
        except Exception:
            return default

    def getlist(self, key: str) -> list[Any]:
        return list(self._data.get(key, []))

    def to_dict(self, flat: bool = True) -> dict[str, Any]:
        if flat:
            return {k: (v[0] if v else None) for k, v in self._data.items()}
        return {k: list(v) for k, v in self._data.items()}

    def items(self):
        for key, values in self._data.items():
            yield key, (values[0] if values else None)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getitem__(self, key: str) -> Any:
        if key not in self._data or not self._data[key]:
            raise KeyError(key)
        return self._data[key][0]

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()

    def __len__(self) -> int:
        return len(self._data)


class FileStorage:
    def __init__(self, filename: str, body: bytes, content_type: str | None = None) -> None:
        self.filename = filename
        self._body = body
        self.content_type = content_type or "application/octet-stream"

    def read(self) -> bytes:
        return self._body


class RequestProxy:
    def _handler(self) -> tornado.web.RequestHandler:
        handler = _current_handler.get()
        if handler is None:
            raise RuntimeError("No active request context")
        return handler

    @property
    def headers(self):
        return self._handler().request.headers

    @property
    def method(self) -> str:
        return self._handler().request.method

    @property
    def path(self) -> str:
        return self._handler().request.path

    @property
    def remote_addr(self) -> str:
        return self._handler().request.remote_ip

    @property
    def content_type(self) -> str | None:
        return self._handler().request.headers.get("Content-Type")

    @property
    def content_length(self) -> int | None:
        value = self._handler().request.headers.get("Content-Length")
        return int(value) if value else None

    @property
    def cookies(self) -> dict[str, str]:
        raw = self._handler().request.headers.get("Cookie", "")
        return tornado.httputil.parse_cookie(raw)

    @property
    def args(self) -> MultiDict:
        handler = self._handler()
        if getattr(handler, "_cached_args", None) is None:
            parsed = {
                key: [v.decode("utf-8") for v in values]
                for key, values in handler.request.query_arguments.items()
            }
            handler._cached_args = MultiDict(parsed)
        return handler._cached_args

    async def get_data(self) -> bytes:
        return self._handler().request.body or b""

    async def get_json(self, force: bool = False, silent: bool = False) -> Any:
        body = await self.get_data()
        if not body:
            return None
        ctype = (self.content_type or "").split(";")[0].strip().lower()
        if not force and ctype != "application/json":
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            if silent:
                return None
            raise

    @property
    def json(self):
        return self.get_json()

    async def _parse_body(self) -> tuple[MultiDict, MultiDict]:
        handler = self._handler()
        if getattr(handler, "_cached_form", None) is not None and getattr(handler, "_cached_files", None) is not None:
            return handler._cached_form, handler._cached_files

        arguments: dict[str, list[bytes]] = {}
        files: dict[str, list[tornado.httputil.HTTPFile]] = {}
        ctype = handler.request.headers.get("Content-Type", "")
        tornado.httputil.parse_body_arguments(
            ctype, handler.request.body, arguments, files
        )
        form_data = {
            key: [v.decode("utf-8") for v in values]
            for key, values in arguments.items()
        }
        file_data: dict[str, list[FileStorage]] = {}
        for key, file_list in files.items():
            file_data[key] = [
                FileStorage(f.filename, f.body, f.content_type) for f in file_list
            ]

        handler._cached_form = MultiDict(form_data)
        handler._cached_files = MultiDict(file_data)
        return handler._cached_form, handler._cached_files

    @property
    def form(self):
        async def _get_form():
            form, _ = await self._parse_body()
            return form

        return _get_form()

    @property
    def files(self):
        async def _get_files():
            _, files = await self._parse_body()
            return files

        return _get_files()

    @property
    def authorization(self):
        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("basic "):
            return None
        try:
            encoded = auth_header.split(None, 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" not in decoded:
                return None
            username, password = decoded.split(":", 1)
            return SimpleNamespace(username=username, password=password)
        except Exception:
            return None


request = RequestProxy()


class SessionProxy(MutableMapping[str, Any]):
    def _handler(self) -> tornado.web.RequestHandler:
        handler = _current_handler.get()
        if handler is None:
            raise RuntimeError("No active request context")
        return handler

    def _data(self) -> dict[str, Any]:
        handler = self._handler()
        return handler._session_data

    def _mark_modified(self) -> None:
        handler = self._handler()
        handler._session_modified = True

    def __getitem__(self, key: str) -> Any:
        return self._data()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data()[key] = value
        self._mark_modified()

    def __delitem__(self, key: str) -> None:
        del self._data()[key]
        self._mark_modified()

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def pop(self, key: str, default: Any = None):
        value = self._data().pop(key, default)
        self._mark_modified()
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data().get(key, default)

    def clear(self) -> None:
        self._data().clear()
        self._mark_modified()

    def __contains__(self, key: object) -> bool:
        return key in self._data()


session = SessionProxy()


class SessionManager:
    def __init__(self, secret_key: str, cookie_name: str = "session", ttl_seconds: int = 86400) -> None:
        self.secret_key = secret_key
        self.cookie_name = cookie_name
        self.ttl_seconds = ttl_seconds

    def _sign(self, value: str) -> str:
        digest = hmac.new(self.secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{value}|{digest}"

    def _unsign(self, signed_value: str) -> str | None:
        if not signed_value or "|" not in signed_value:
            return None
        value, digest = signed_value.rsplit("|", 1)
        expected = hmac.new(self.secret_key.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            return None
        return value

    def _redis(self):
        from rag.utils.redis_conn import REDIS_CONN
        return REDIS_CONN

    def load_session(self, handler: tornado.web.RequestHandler) -> tuple[str | None, dict[str, Any]]:
        raw = handler.request.headers.get("Cookie", "")
        cookies = tornado.httputil.parse_cookie(raw)
        signed_id = cookies.get(self.cookie_name)
        session_id = self._unsign(signed_id) if signed_id else None
        if not session_id:
            return None, {}
        data = self._redis().get(f"session:{session_id}")
        if not data:
            return session_id, {}
        try:
            return session_id, json.loads(data)
        except Exception:
            return session_id, {}

    def save_session(
        self, handler: tornado.web.RequestHandler, session_id: str | None, data: dict[str, Any], modified: bool
    ) -> str | None:
        if not modified:
            return session_id
        if not session_id:
            session_id = uuid.uuid4().hex
        self._redis().set(f"session:{session_id}", json.dumps(data), self.ttl_seconds)
        signed_id = self._sign(session_id)
        handler.set_cookie(self.cookie_name, signed_id, httponly=True)
        return session_id


class Route:
    def __init__(self, path: str) -> None:
        self.path = path
        self.method_map: dict[str, Callable[..., Any]] = {}

    def add_methods(self, methods: list[str], func: Callable[..., Any]) -> None:
        for method in methods:
            self.method_map[method.upper()] = func

    @property
    def methods(self) -> list[str]:
        return list(self.method_map.keys())


class Blueprint:
    def __init__(self, name: str, import_name: str) -> None:
        self.name = name
        self.import_name = import_name
        self.routes: list[Route] = []

    def route(self, path: str, methods: list[str] | None = None):
        methods = methods or ["GET"]

        def decorator(func: Callable[..., Any]):
            existing = next((r for r in self.routes if r.path == path), None)
            if existing:
                existing.add_methods(methods, func)
            else:
                route = Route(path)
                route.add_methods(methods, func)
                self.routes.append(route)
            return func

        return decorator


class QuartSchema:
    def __init__(self, _app: "Quart") -> None:
        return None


def cors(app: "Quart", allow_origin: str = "*") -> "Quart":
    app.cors_allow_origin = allow_origin
    return app


class Quart:
    def __init__(self, import_name: str) -> None:
        self.import_name = import_name
        self.config: dict[str, Any] = {}
        self.json_encoder = None
        self.url_map = URLMap()
        self.secret_key: str | None = None
        self._routes: list[Route] = []
        self._error_handlers: dict[type[BaseException], Callable[..., Any]] = {}
        self._status_handlers: dict[int, Callable[..., Any]] = {}
        self._teardown_handlers: list[Callable[..., Any]] = []
        self.cli = CLI()
        self.cors_allow_origin: str | None = None
        self.session_manager: SessionManager | None = None

    def errorhandler(self, exc_type: type[BaseException] | int):
        def decorator(func: Callable[..., Any]):
            if isinstance(exc_type, int):
                self._status_handlers[exc_type] = func
            else:
                self._error_handlers[exc_type] = func
            return func

        return decorator

    def route(self, path: str, methods: list[str] | None = None):
        methods = methods or ["GET"]

        def decorator(func: Callable[..., Any]):
            existing = next((r for r in self._routes if r.path == path), None)
            if existing:
                existing.add_methods(methods, func)
            else:
                route = Route(path)
                route.add_methods(methods, func)
                self._routes.append(route)
            return func

        return decorator

    def teardown_request(self, func: Callable[..., Any]):
        self._teardown_handlers.append(func)
        return func

    def register_blueprint(self, blueprint: Blueprint, url_prefix: str | None = None) -> None:
        prefix = url_prefix or ""
        for route in blueprint.routes:
            path = f"{prefix}{route.path}"
            existing = next((r for r in self._routes if r.path == path), None)
            if existing:
                for method, func in route.method_map.items():
                    existing.add_methods([method], func)
            else:
                new_route = Route(path)
                for method, func in route.method_map.items():
                    new_route.add_methods([method], func)
                self._routes.append(new_route)

    def ensure_async(self, func: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(func):
            return func

        async def wrapper(*args: Any, **kwargs: Any):
            return func(*args, **kwargs)

        return wrapper

    def _compile_path(self, path: str) -> str:
        parts: list[str] = []
        last = 0
        for match in re.finditer(r"<([a-zA-Z_][^>]*)>", path):
            parts.append(re.escape(path[last:match.start()]))
            name = match.group(1)
            parts.append(f"(?P<{name}>[^/]+)")
            last = match.end()
        parts.append(re.escape(path[last:]))
        regex = "".join(parts)
        if not self.url_map.strict_slashes:
            if regex.endswith("/"):
                regex = regex.rstrip("/")
            regex = f"{regex}/?"
        return f"^{regex}$"

    def _build_tornado_app(self) -> tornado.web.Application:
        handlers: list[tuple[str, type[tornado.web.RequestHandler], dict[str, Any]]] = []
        for route in self._routes:
            pattern = self._compile_path(route.path)
            handlers.append(
                (
                    pattern,
                    TornadoRouteHandler,
                    {"route": route, "app": self},
                )
            )
        return tornado.web.Application(
            handlers,
            debug=self.config.get("DEBUG", False),
            default_handler_class=TornadoNotFoundHandler,
            default_handler_args={"app": self},
        )

    def run(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        if not self.session_manager:
            secret = self.secret_key or self.config.get("SECRET_KEY") or "ragflow"
            self.session_manager = SessionManager(secret_key=secret)
        app = self._build_tornado_app()
        max_body_size = int(self.config.get("MAX_CONTENT_LENGTH", 1024 * 1024 * 1024))
        body_timeout = int(self.config.get("BODY_TIMEOUT", 600))
        idle_timeout = int(self.config.get("RESPONSE_TIMEOUT", 600))
        server = tornado.httpserver.HTTPServer(
            app,
            max_body_size=max_body_size,
            body_timeout=body_timeout,
            idle_connection_timeout=idle_timeout,
        )
        server.listen(port, address=host)
        tornado.ioloop.IOLoop.current().start()


class TornadoRouteHandler(tornado.web.RequestHandler):
    def initialize(self, route: Route, app: Quart) -> None:
        self._route = route
        self._app = app
        self._session_id: str | None = None
        self._session_data: dict[str, Any] = {}
        self._session_modified = False
        self._cached_args: MultiDict | None = None
        self._cached_form: MultiDict | None = None
        self._cached_files: MultiDict | None = None
        self._last_exception: Exception | None = None

    def set_default_headers(self) -> None:
        app = getattr(self, "_app", None)
        if app and app.cors_allow_origin:
            self.set_header("Access-Control-Allow-Origin", app.cors_allow_origin)
            self.set_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Requested-With")
            self.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")

    async def prepare(self) -> None:
        _current_handler.set(self)
        _current_app.set(self._app)
        _current_g.set(SimpleNamespace())
        if self._app.session_manager:
            self._session_id, self._session_data = self._app.session_manager.load_session(self)

    async def options(self, *args: Any, **kwargs: Any) -> None:
        self.set_status(204)
        self.finish()

    async def _execute_route(self, *args: Any, **kwargs: Any) -> None:
        try:
            method = self.request.method.upper()
            if method not in self._route.method_map:
                self.set_status(405)
                self.finish()
                return
            result = self._route.method_map[method](*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            self._last_exception = exc
            handler = self._app._error_handlers.get(Exception)
            if handler:
                result = handler(exc)
                if asyncio.iscoroutine(result):
                    result = await result
            else:
                raise
        await self._write_result(result)

    async def _write_result(self, result: Any) -> None:
        status = None
        headers: Mapping[str, str] | None = None
        if isinstance(result, tuple):
            if len(result) == 2:
                result, status = result
            elif len(result) == 3:
                result, status, headers = result

        if isinstance(result, Response):
            if status is not None:
                result.status_code = status
            self.set_status(result.status_code)
            for key, value in result.headers.items():
                self.set_header(key, value)
            if "Content-Type" not in result.headers:
                if result.content_type:
                    self.set_header("Content-Type", result.content_type)
                elif result.mimetype:
                    self.set_header("Content-Type", result.mimetype)
            await self._write_body(result.body)
            return

        if headers:
            for key, value in headers.items():
                self.set_header(key, value)
        if status is not None:
            self.set_status(status)

        if isinstance(result, (dict, list)):
            resp = jsonify(result)
            self.set_header("Content-Type", "application/json")
            await self._write_body(resp.body)
            return

        await self._write_body(result)

    async def _write_body(self, body: Any) -> None:
        if body is None:
            self.finish()
            return
        if hasattr(body, "__aiter__"):
            async for chunk in body:
                self.write(_ensure_bytes(chunk))
                await self.flush()
            self.finish()
            return
        if isinstance(body, Iterable) and not isinstance(body, (bytes, bytearray, str, dict)):
            for chunk in body:
                self.write(_ensure_bytes(chunk))
                await self.flush()
            self.finish()
            return
        self.write(_ensure_bytes(body))
        self.finish()

    async def _async_on_finish(self) -> None:
        for teardown in self._app._teardown_handlers:
            try:
                result = teardown(self._last_exception)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        if self._app.session_manager:
            self._session_id = self._app.session_manager.save_session(
                self, self._session_id, self._session_data, self._session_modified
            )
        _current_handler.set(None)
        _current_app.set(None)
        _current_g.set(None)

    def on_finish(self) -> None:
        tornado.ioloop.IOLoop.current().spawn_callback(self._async_on_finish)

    async def get(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)

    async def post(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)

    async def put(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)

    async def patch(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)

    async def head(self, *args: Any, **kwargs: Any) -> None:
        await self._execute_route(*args, **kwargs)


class TornadoNotFoundHandler(tornado.web.RequestHandler):
    def initialize(self, app: Quart) -> None:
        self._app = app
        self._session_id: str | None = None
        self._session_data: dict[str, Any] = {}
        self._session_modified = False

    def set_default_headers(self) -> None:
        app = getattr(self, "_app", None)
        if app and app.cors_allow_origin:
            self.set_header("Access-Control-Allow-Origin", app.cors_allow_origin)
            self.set_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Requested-With")
            self.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")

    async def prepare(self) -> None:
        _current_handler.set(self)
        _current_app.set(self._app)
        _current_g.set(SimpleNamespace())
        if self._app.session_manager:
            self._session_id, self._session_data = self._app.session_manager.load_session(self)

    async def _write_result(self, result: Any) -> None:
        status = None
        headers: Mapping[str, str] | None = None
        if isinstance(result, tuple):
            if len(result) == 2:
                result, status = result
            elif len(result) == 3:
                result, status, headers = result

        if isinstance(result, Response):
            if status is not None:
                result.status_code = status
            self.set_status(result.status_code)
            for key, value in result.headers.items():
                self.set_header(key, value)
            if "Content-Type" not in result.headers:
                if result.content_type:
                    self.set_header("Content-Type", result.content_type)
                elif result.mimetype:
                    self.set_header("Content-Type", result.mimetype)
            await self._write_body(result.body)
            return

        if headers:
            for key, value in headers.items():
                self.set_header(key, value)
        if status is not None:
            self.set_status(status)

        if isinstance(result, (dict, list)):
            resp = jsonify(result)
            self.set_header("Content-Type", "application/json")
            await self._write_body(resp.body)
            return

        await self._write_body(result)

    async def _write_body(self, body: Any) -> None:
        if body is None:
            self.finish()
            return
        if hasattr(body, "__aiter__"):
            async for chunk in body:
                self.write(_ensure_bytes(chunk))
                await self.flush()
            self.finish()
            return
        if isinstance(body, Iterable) and not isinstance(body, (bytes, bytearray, str, dict)):
            for chunk in body:
                self.write(_ensure_bytes(chunk))
                await self.flush()
            self.finish()
            return
        self.write(_ensure_bytes(body))
        self.finish()

    async def _handle_not_found(self) -> None:
        handler = self._app._status_handlers.get(404)
        if handler:
            result = handler(self)
            if asyncio.iscoroutine(result):
                result = await result
            await self._write_result(result)
            return
        self.set_status(404)
        self.finish()

    async def _async_on_finish(self) -> None:
        if self._app.session_manager:
            self._session_id = self._app.session_manager.save_session(
                self, self._session_id, self._session_data, self._session_modified
            )
        _current_handler.set(None)
        _current_app.set(None)
        _current_g.set(None)

    def on_finish(self) -> None:
        tornado.ioloop.IOLoop.current().spawn_callback(self._async_on_finish)

    async def get(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

    async def post(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

    async def put(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

    async def patch(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

    async def head(self, *args: Any, **kwargs: Any) -> None:
        await self._handle_not_found()

def _get_g() -> SimpleNamespace:
    g = _current_g.get()
    if g is None:
        g = SimpleNamespace()
        _current_g.set(g)
    return g


class GProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_get_g(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(_get_g(), name, value)


g = GProxy()


class CurrentAppProxy:
    def __getattr__(self, name: str) -> Any:
        app = _current_app.get()
        if app is None:
            raise RuntimeError("No active app context")
        return getattr(app, name)


current_app = CurrentAppProxy()
