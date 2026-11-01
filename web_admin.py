"""Loopback-only management application; all writes use KnowledgeStore."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import secrets
import socket
import sqlite3
import threading
import time
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn

from knowledge_store import KnowledgeStore, StoreError, FULL_BYTES
from config import ConfigError, config_path, save_root
import crashlog

LOGGER = logging.getLogger('knowledge.web')
ASSETS = Path(__file__).resolve().parent / 'web'


class Forbidden(Exception):
    """Authorization failure, mapped to HTTP 403."""


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['create', 'update', 'delete', 'move', 'restore', 'patch']
    path: str = ''
    content: str = ''
    target: str = ''
    expected_version: str = ''
    record_id: str = ''
    start_byte: int = 0
    end_byte: int = 0


class IndexRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    full: bool = False


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    ticket: str


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    root: str
    create: bool = False


class CrashRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['clear']


class WebAdmin:
    def __init__(self, state, port=0):
        self.state = state
        self.guard = threading.Lock()
        self.tickets = {}
        self.sessions = {}
        self.index_job = dict(state='idle', report=None)
        self.jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix='index')
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(('127.0.0.1', port))
        self.socket.listen(128)
        self.port = self.socket.getsockname()[1]
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.app = Starlette(routes=[
            Route('/', self.asset), Route('/assets/{name}', self.asset),
            Route('/api/session', self.session, methods=['GET', 'POST']),
            Route('/api/setup', self.setup, methods=['GET', 'POST']),
            Route('/api/status', self.status),
            Route('/api/crashes', self.crashes, methods=['GET', 'POST']),
            Route('/api/tree', self.tree, methods=['GET', 'POST']),
            Route('/api/documents', self.documents, methods=['GET', 'POST']),
            Route('/api/trash', self.trash, methods=['GET', 'POST']),
            Route('/api/index', self.index, methods=['GET', 'POST']),
        ])
        self.app.add_middleware(BaseHTTPMiddleware, dispatch=self.boundary)
        self.app.add_middleware(GZipMiddleware, minimum_size=1024)
        config = uvicorn.Config(self.app, host='127.0.0.1', port=self.port, log_config=None,
                                access_log=False, timeout_graceful_shutdown=15, ws='none')
        self.server = uvicorn.Server(config)
        self.thread = None

    def open_url(self):
        token = secrets.token_urlsafe(32)
        with self.guard:
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v > now}
            if len(self.tickets) >= 32:
                self.tickets.pop(next(iter(self.tickets)))
            self.tickets[token] = now + 60
        return self.base_url + '/#ticket=' + token

    async def boundary(self, request, call_next):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                   'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
        try:
            if request.headers.get('host') != f'127.0.0.1:{self.port}':
                raise Forbidden('不允许的 Host')
            origin = request.headers.get('origin')
            if origin and origin != self.base_url:
                raise Forbidden('不允许的跨站请求')
            if request.method not in ('GET', 'HEAD') and origin != self.base_url:
                raise Forbidden('写操作缺少有效 Origin')
            if self.state.stopping.is_set():
                return JSONResponse(dict(ok=False, error='服务正在退出'), status_code=503, headers=headers)
            if request.url.path.startswith('/api/') and request.url.path != '/api/session':
                session = self._session(request)
                if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(
                        request.headers.get('x-csrf-token', ''), session['csrf']):
                    raise Forbidden('写操作验证失败，请重新打开管理页面')
            result = await call_next(request)
        except Forbidden as exc:
            result = JSONResponse(dict(ok=False, error=str(exc)), status_code=403)
        except (ValidationError, ValueError, TypeError) as exc:
            # Do not echo validation inputs (which can contain document text).
            result = JSONResponse(dict(ok=False, error='请求参数无效，请检查操作与字段'), status_code=400)
        except FileExistsError as exc:
            result = JSONResponse(dict(ok=False, error='目标路径已存在，拒绝覆盖'), status_code=409)
        except FileNotFoundError:
            result = JSONResponse(dict(ok=False, error='文档、目录或回收记录不存在'), status_code=404)
        except StoreError as exc:
            result = JSONResponse(dict(ok=False, error=str(exc)), status_code=409)
        except (OSError, sqlite3.Error) as exc:
            LOGGER.error('文件或数据库操作失败: %s', type(exc).__name__)
            result = JSONResponse(dict(ok=False, error=f'文件或数据库操作失败（{type(exc).__name__}），请检查权限与占用情况'), status_code=503)
        except Exception as exc:
            LOGGER.error('管理接口失败: %s', type(exc).__name__)
            result = JSONResponse(dict(ok=False, error='管理接口异常，请查看服务日志'), status_code=500)
        result.headers.update(headers)
        return result

    def _session(self, request):
        token = request.cookies.get('knowledge_session', '')
        with self.guard:
            session = self.sessions.get(token)
            if not session or session['expires'] < time.monotonic():
                self.sessions.pop(token, None)
                raise Forbidden('请从托盘重新打开管理页面')
            return session

    async def body(self, request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > FULL_BYTES * 6 + 65536:
                raise StoreError('请求内容过大')
        return json.loads(data)

    def store(self):
        store = self.state.store
        if store is None:
            raise StoreError('尚未配置知识库，请先完成首次设置')
        return store

    async def setup(self, request):
        if request.method == 'GET':
            return JSONResponse(dict(configured=self.state.store is not None,
                                     config_path=str(config_path()),
                                     error=self.state.config_error))
        body = SetupRequest.model_validate(await self.body(request))
        with self.guard:
            if self.state.store is not None:
                raise StoreError('已配置知识库，如需更换请退出托盘并修改配置文件')
            if self.state.configuring:
                raise StoreError('正在配置知识库，请稍候')
            self.state.configuring = True
        store = None
        try:
            path = Path(body.root.strip())
            if not path.is_absolute():
                raise StoreError('请输入绝对路径，例如 D:\\知识库')
            if not path.exists():
                if not body.create:
                    raise StoreError('目录不存在；请先创建，或勾选“目录不存在时创建”')
                path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise StoreError('路径不是目录')
            store = await anyio.to_thread.run_sync(KnowledgeStore, path)
            try:
                save_root(store.root)
            except ConfigError as exc:
                raise StoreError(f'配置保存失败: {exc}') from exc
            with self.guard:
                if self.state.store is not None:
                    raise StoreError('已配置知识库，请刷新页面')
                self.state.store = store
                self.state.config_error = None
                configured = store
                store = None
            if self.state.tray:
                try:
                    self.state.tray.refresh()
                except Exception:
                    LOGGER.exception('托盘提示更新失败')
            return JSONResponse(dict(ok=True, root=str(configured.root),
                                     config_path=str(config_path())))
        finally:
            if store is not None:
                store.close()
            with self.guard:
                self.state.configuring = False

    async def session(self, request):
        if request.method == 'GET':
            return JSONResponse(dict(ok=True, csrf=self._session(request)['csrf']))
        body = TicketRequest.model_validate(await self.body(request))
        with self.guard:
            deadline = self.tickets.pop(body.ticket, 0)
            if deadline < time.monotonic():
                raise Forbidden('打开票据无效或已过期，请从托盘重新打开')
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = time.monotonic()
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[token] = dict(csrf=csrf, expires=now + 86400)
        response = JSONResponse(dict(ok=True, csrf=csrf))
        response.set_cookie('knowledge_session', token, max_age=86400, httponly=True, samesite='strict')
        return response

    async def asset(self, request):
        name = request.path_params.get('name', 'index.html')
        if name not in ('index.html', 'app.js', 'style.css', 'book.svg'):
            return Response(status_code=404)
        return FileResponse(ASSETS / name)

    async def status(self, request):
        return JSONResponse(await anyio.to_thread.run_sync(self.state.status))

    def current_root(self):
        store = self.state.store
        return store.root if store is not None else None

    async def crashes(self, request):
        root = self.current_root()
        if request.method == 'GET':
            items = await anyio.to_thread.run_sync(lambda: crashlog.records(5, root))
            return JSONResponse(dict(ok=True, dir=str(crashlog.crash_folder(root)), items=items))
        CrashRequest.model_validate(await self.body(request))
        removed = await anyio.to_thread.run_sync(lambda: crashlog.clear(root))
        return JSONResponse(dict(ok=True, removed=removed))

    async def documents(self, request):
        store = self.store()
        if request.method == 'GET':
            query = request.query_params
            if 'path' in query:
                def read():
                    large = (query.get('auto') == 'true'
                             and store._path(query['path']).stat().st_size > FULL_BYTES)
                    if 'line' in query and large:
                        return store.read_document_chunk_at_line(query['path'], int(query['line']))
                    if 'offset' in query or large:
                        return store.read_document_chunk(query['path'], int(query.get('offset', 0)))
                    return store.read_full_document(query['path'])
                data = await anyio.to_thread.run_sync(read)
            else:
                options = dict(directory=query.get('directory', ''),
                               recursive=query.get('recursive', 'true') == 'true',
                               offset=int(query.get('offset', 0)), limit=int(query.get('limit', 20)))
                if query.get('query', '').strip():
                    data = await anyio.to_thread.run_sync(lambda: store.search_documents(query['query'], **options))
                else:
                    data = await anyio.to_thread.run_sync(lambda: store.list_documents(**options))
            return JSONResponse(data)
        body = WriteRequest.model_validate(await self.body(request))
        actions = {
            'create': lambda: store.create_document(body.path, body.content),
            'update': lambda: store.update_document(body.path, body.content, body.expected_version),
            'delete': lambda: store.delete_document(body.path, body.expected_version),
            'move': lambda: store.move_entry(body.path, body.target, body.expected_version),
            'patch': lambda: store.patch_document(body.path, body.start_byte, body.end_byte, body.content, body.expected_version),
        }
        if body.action not in actions:
            raise ValueError('不支持的文档操作')
        return JSONResponse(await anyio.to_thread.run_sync(actions[body.action]))

    async def tree(self, request):
        store = self.store()
        if request.method == 'GET':
            return JSONResponse(await anyio.to_thread.run_sync(store.get_tree))
        body = WriteRequest.model_validate(await self.body(request))
        actions = {
            'create': lambda: store.create_directory(body.path),
            'move': lambda: store.move_entry(body.path, body.target, body.expected_version),
            'delete': lambda: store.delete_directory(body.path, body.expected_version),
        }
        if body.action not in actions:
            raise ValueError('不支持的目录操作')
        return JSONResponse(await anyio.to_thread.run_sync(actions[body.action]))

    async def trash(self, request):
        store = self.store()
        if request.method == 'GET':
            offset = int(request.query_params.get('offset', 0))
            limit = int(request.query_params.get('limit', 20))
            return JSONResponse(await anyio.to_thread.run_sync(lambda: store.list_deleted(offset, limit)))
        body = WriteRequest.model_validate(await self.body(request))
        if body.action != 'restore':
            raise ValueError('回收站仅支持恢复')
        return JSONResponse(await anyio.to_thread.run_sync(lambda: store.restore_document(body.record_id)))

    async def index(self, request):
        if request.method == 'GET':
            with self.guard:
                return JSONResponse(dict(self.index_job))
        body = IndexRequest.model_validate(await self.body(request))
        self.store()
        with self.guard:
            if self.index_job['state'] == 'running':
                raise StoreError('已有索引任务正在执行')
            self.index_job = dict(state='running', report=None)
        self.jobs.submit(self._refresh, body.full)
        return JSONResponse(dict(state='running'), status_code=202)

    def _refresh(self, full):
        store = self.state.store
        if store is None:
            result = dict(state='failed', report=dict(ok=False, error='尚未配置知识库'))
        else:
            try:
                report = store.refresh_index(full)
                result = dict(state='completed' if report['ok'] else 'failed', report=report)
            except Exception as exc:
                LOGGER.error('索引任务失败: %s', type(exc).__name__)
                result = dict(state='failed', report=dict(ok=False, error=f'索引任务失败（{type(exc).__name__}）'))
        with self.guard:
            self.index_job = result

    def start(self):
        def run():
            try:
                asyncio.run(self.server.serve(sockets=[self.socket]))
            except Exception as exc:
                LOGGER.exception('本地网页服务失败')
                crashlog.record_current(exc, note='本地网页服务失败，服务停止')
                self.state.request_stop()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise StoreError('本地网页服务启动失败')
            time.sleep(0.05)

    def close(self):
        self.jobs.shutdown(wait=True, cancel_futures=True)
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise StoreError('网页服务未能在 20 秒内退出')
        self.socket.close()
