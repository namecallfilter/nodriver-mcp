"""Safe script selection, deterministic hot loading, and scraper run helpers."""

from __future__ import annotations

import ast
import asyncio
import builtins
import hashlib
import inspect
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
import weakref
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from nodriver_mcp.browser import (
    close_page_with_browser,
    create_new_page,
    evaluate_json,
    navigate_page,
    tab_id,
    validate_web_url,
)
from nodriver_mcp.config import Settings

logger = logging.getLogger(__name__)

_SCRAPER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ARTIFACT_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_SCRAPER_CHILD_TASKS: ContextVar[set[asyncio.Future[Any]] | None] = ContextVar(
    "nodriver_mcp_scraper_child_tasks", default=None
)


@dataclass
class _TaskFactoryLease:
    """Share one task-factory dispatcher across overlapping scraper runs."""

    previous_factory: Any
    dispatcher: Any = None
    users: int = 0
    active_sets: dict[int, set[asyncio.Future[Any]]] = field(default_factory=dict)


_TASK_FACTORY_LEASES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _TaskFactoryLease] = (
    weakref.WeakKeyDictionary()
)


def _install_task_tracking(
    loop: asyncio.AbstractEventLoop,
    child_tasks: set[asyncio.Future[Any]],
) -> _TaskFactoryLease:
    """Install or share a context-aware task tracker on an event loop."""
    existing = _TASK_FACTORY_LEASES.get(loop)
    if existing is not None and loop.get_task_factory() is existing.dispatcher:
        existing.users += 1
        existing.active_sets[id(child_tasks)] = child_tasks
        return existing

    previous_factory = loop.get_task_factory()
    lease = _TaskFactoryLease(previous_factory=previous_factory, users=1)

    def tracking_task_factory(
        task_loop: asyncio.AbstractEventLoop,
        coroutine: Any,
        **kwargs: Any,
    ) -> asyncio.Future[Any]:
        if previous_factory is None:
            future: asyncio.Future[Any] = asyncio.Task(coroutine, loop=task_loop, **kwargs)
        else:
            future = previous_factory(task_loop, coroutine, **kwargs)
        active = _SCRAPER_CHILD_TASKS.get()
        if (
            active is not None
            and lease.active_sets.get(id(active)) is active
            and isinstance(future, asyncio.Future)
        ):
            active.add(future)
        return future

    lease.dispatcher = tracking_task_factory
    lease.active_sets[id(child_tasks)] = child_tasks
    _TASK_FACTORY_LEASES[loop] = lease
    loop.set_task_factory(tracking_task_factory)
    return lease


def _release_task_tracking(
    loop: asyncio.AbstractEventLoop,
    lease: _TaskFactoryLease,
    child_tasks: set[asyncio.Future[Any]],
) -> None:
    """Release a shared task tracker without clobbering a newer external factory."""
    lease.active_sets.pop(id(child_tasks), None)
    lease.users -= 1
    if lease.users > 0:
        return
    if loop.get_task_factory() is lease.dispatcher:
        loop.set_task_factory(lease.previous_factory)
    if _TASK_FACTORY_LEASES.get(loop) is lease:
        _TASK_FACTORY_LEASES.pop(loop, None)


class ScraperError(RuntimeError):
    """Raised for invalid scraper files or failed scraper execution."""


def _revision(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()[:16]


def _validate_name(name: str) -> str:
    if not _SCRAPER_NAME.fullmatch(name) or name.upper() in _WINDOWS_RESERVED:
        raise ScraperError(
            "Scraper name must start with a letter and contain at most 64 letters, "
            "numbers, underscores, or hyphens"
        )
    return name


def _validate_source(source: bytes, filename: str) -> Any:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScraperError("Scraper source must be UTF-8") from exc
    try:
        tree = ast.parse(text, filename=filename)
        code = compile(tree, filename, "exec")
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno else "unknown line"
        raise ScraperError(f"Scraper has a syntax error at {location}: {exc.msg}") from exc
    entry_points = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "scrape"
    ]
    if not entry_points:
        raise ScraperError("Scraper must define `async def scrape(ctx, params): ...`")
    return code


class ScriptStore:
    """Resolve only direct-child scraper files and write them atomically."""

    def __init__(self, root: Path, *, max_script_bytes: int) -> None:
        self.root = root.resolve()
        self.max_script_bytes = max_script_bytes

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str, *, must_exist: bool = False) -> Path:
        safe_name = _validate_name(name)
        self.ensure_root()
        candidate = self.root / f"{safe_name}.py"
        if candidate.exists():
            resolved = candidate.resolve()
            if resolved.parent != self.root or candidate.is_symlink():
                raise ScraperError("Scraper path resolves outside the configured scripts directory")
        elif candidate.parent.resolve() != self.root:
            raise ScraperError("Scraper path resolves outside the configured scripts directory")
        if must_exist and not candidate.is_file():
            raise ScraperError(f"Scraper {safe_name!r} does not exist")
        return candidate

    def read_bytes(self, name: str) -> tuple[Path, bytes]:
        path = self.path_for(name, must_exist=True)
        source = path.read_bytes()
        if len(source) > self.max_script_bytes:
            raise ScraperError(
                f"Scraper is {len(source)} bytes; limit is {self.max_script_bytes} bytes"
            )
        return path, source

    def read(self, name: str) -> dict[str, Any]:
        path, source = self.read_bytes(name)
        return {
            "name": _validate_name(name),
            "revision": _revision(source),
            "bytes": len(source),
            "source": source.decode("utf-8"),
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        }

    def list(self) -> list[dict[str, Any]]:
        self.ensure_root()
        records: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.py"), key=lambda item: item.name.lower()):
            try:
                name = _validate_name(path.stem)
                if path.is_symlink() or path.resolve().parent != self.root:
                    continue
                source = path.read_bytes()
                if len(source) > self.max_script_bytes:
                    records.append(
                        {
                            "name": name,
                            "error": "too_large",
                            "bytes": len(source),
                        }
                    )
                    continue
                records.append(
                    {
                        "name": name,
                        "revision": _revision(source),
                        "bytes": len(source),
                        "modified_at": datetime.fromtimestamp(
                            path.stat().st_mtime, UTC
                        ).isoformat(),
                    }
                )
            except (OSError, ScraperError):
                continue
        return records

    def save(
        self,
        name: str,
        source: str,
        *,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        path = self.path_for(name)
        encoded = source.encode("utf-8")
        if len(encoded) > self.max_script_bytes:
            raise ScraperError(
                f"Scraper is {len(encoded)} bytes; limit is {self.max_script_bytes} bytes"
            )
        _validate_source(encoded, str(path))
        if expected_revision is not None:
            if not path.exists():
                raise ScraperError("expected_revision was supplied but the scraper does not exist")
            current = _revision(path.read_bytes())
            if current != expected_revision:
                raise ScraperError(
                    "Scraper changed since it was read "
                    f"(expected {expected_revision}, found {current})"
                )

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{path.stem}-", suffix=".tmp", dir=self.root, delete=False
            ) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return self.read(name)


@dataclass(slots=True)
class ScrapeContext:
    """Context passed to each generated scraper.

    ``browser`` and ``page`` are raw nodriver objects for full-speed bulk operations.
    Helper-created tabs are tracked and closed automatically at the end of the run.
    """

    browser: Any
    page: Any
    settings: Settings
    scraper_name: str
    _extra_tabs: list[Any] = field(default_factory=list)
    _extra_tab_reservations: int = 0

    async def _without_child_task_tracking(self, awaitable: Any) -> Any:
        token = _SCRAPER_CHILD_TASKS.set(None)
        try:
            return await awaitable
        finally:
            _SCRAPER_CHILD_TASKS.reset(token)

    async def goto(self, url: str) -> Any:
        """Navigate the run's primary page and return the raw nodriver tab."""
        self.page = await self._without_child_task_tracking(
            navigate_page(
                self.page,
                validate_web_url(url),
                timeout_seconds=self.settings.navigation_timeout_seconds,
            )
        )
        return self.page

    async def new_page(self, url: str = "about:blank") -> Any:
        """Open and track a cookie-sharing tab, subject to the configured tab limit."""
        if len(self._extra_tabs) + self._extra_tab_reservations >= self.settings.max_tabs_per_run:
            raise ScraperError(
                f"This run may open at most {self.settings.max_tabs_per_run} extra tabs"
            )
        self._extra_tab_reservations += 1
        try:
            page = await self._without_child_task_tracking(
                create_new_page(
                    self.browser,
                    validate_web_url(url),
                    timeout_seconds=self.settings.navigation_timeout_seconds,
                )
            )
        finally:
            self._extra_tab_reservations -= 1
        self._extra_tabs.append(page)
        return page

    async def evaluate_json(self, expression: str, page: Any | None = None) -> Any:
        """Evaluate a JavaScript expression and return its JSON-compatible value."""
        return await self._without_child_task_tracking(evaluate_json(page or self.page, expression))

    async def write_artifact(
        self,
        data: Any,
        *,
        label: str = "results",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Atomically persist a large JSON or text result below the artifacts directory."""
        if not _ARTIFACT_LABEL.fullmatch(label):
            raise ScraperError("Artifact label may contain only letters, numbers, _ and -")
        if output_format == "json":
            try:
                payload = await asyncio.to_thread(
                    lambda: json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode(
                        "utf-8"
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ScraperError(f"Artifact is not valid JSON: {exc}") from exc
            suffix = "json"
        elif output_format == "text":
            if not isinstance(data, str):
                raise ScraperError("Text artifacts require a string value")
            payload = data.encode("utf-8")
            suffix = "txt"
        else:
            raise ScraperError("Artifact format must be 'json' or 'text'")

        return await self._write_artifact_payload(
            payload, label=label, suffix=suffix, output_format=output_format
        )

    async def _write_artifact_payload(
        self,
        payload: bytes,
        *,
        label: str,
        suffix: str,
        output_format: str,
    ) -> dict[str, Any]:
        if len(payload) > self.settings.max_artifact_result_bytes:
            raise ScraperError(
                f"Artifact is {len(payload)} bytes; limit is "
                f"{self.settings.max_artifact_result_bytes} bytes"
            )

        root = self.settings.artifacts_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        filename = f"{self.scraper_name}-{label}-{timestamp}-{uuid.uuid4().hex[:8]}.{suffix}"
        path = root / filename

        def write() -> None:
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=f".{filename}-", suffix=".tmp", dir=root, delete=False
                ) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary = Path(handle.name)
                os.replace(temporary, path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()

        await asyncio.to_thread(write)
        return {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "format": output_format,
        }

    async def close_extra_tabs(self) -> None:
        tabs, self._extra_tabs = list(reversed(self._extra_tabs)), []
        await asyncio.gather(
            *(close_page_with_browser(self.browser, page) for page in tabs),
            return_exceptions=True,
        )


class _PrintCapture:
    def __init__(self, max_chars: int = 16_000) -> None:
        self.max_chars = max_chars
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def print(
        self,
        *values: object,
        sep: str = " ",
        end: str = "\n",
        file: Any | None = None,
        flush: bool = False,
    ) -> None:
        del flush
        if file is not None and file is not sys.stdout and file is not sys.stderr:
            builtins.print(*values, sep=sep, end=end, file=file)
            return
        text = sep.join(str(value) for value in values) + end
        remaining = self.max_chars - self._length
        if remaining > 0:
            part = text[:remaining]
            self._parts.append(part)
            self._length += len(part)
        if len(text) > remaining:
            self.truncated = True

    @property
    def value(self) -> str:
        return "".join(self._parts)


class ScriptRunner:
    """Load source bytes fresh on every call and run them against a shared browser."""

    def __init__(self, store: ScriptStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    @staticmethod
    def _connection_runtime_tasks(context: ScrapeContext) -> set[asyncio.Future[Any]]:
        """Find listener/keepalive tasks that belong to live browser connections."""
        browser = context.browser
        connections = [browser, context.page, *context._extra_tabs]
        connections.extend(list(getattr(browser, "targets", [])))
        protected: set[asyncio.Future[Any]] = set()
        seen: set[int] = set()
        for connection in connections:
            if id(connection) in seen:
                continue
            seen.add(id(connection))
            socket = getattr(connection, "socket", None)
            candidates = (
                getattr(connection, "_listener_task", None),
                getattr(socket, "keepalive_task", None),
            )
            protected.update(task for task in candidates if isinstance(task, asyncio.Future))
        return protected

    @staticmethod
    async def _await_cleanup_despite_cancellation(
        cleanup: asyncio.Task[None],
    ) -> None:
        """Keep cleanup owned through its hard deadline, then restore cancellation."""
        interrupted = False
        current = asyncio.current_task()
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
                if current is not None:
                    current.uncancel()
            except Exception:
                break
        with suppress(BaseException):
            cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _cleanup_run(
        self,
        context: ScrapeContext,
        child_tasks: set[asyncio.Future[Any]],
    ) -> None:
        current = asyncio.current_task()
        cleanup_deadline = asyncio.get_running_loop().time() + 2
        cancelled_tasks: set[asyncio.Future[Any]] = set()
        pending: set[asyncio.Future[Any]] = set()
        while True:
            protected_tasks = self._connection_runtime_tasks(context)
            script_tasks = {
                task
                for task in set(child_tasks)
                if task is not current and task not in protected_tasks
            }
            for task in script_tasks:
                if task.done():
                    with suppress(BaseException):
                        task.exception()
                elif task not in cancelled_tasks:
                    task.cancel()
                    cancelled_tasks.add(task)
            pending = {task for task in script_tasks if not task.done()}
            remaining = cleanup_deadline - asyncio.get_running_loop().time()
            if not pending or remaining <= 0:
                # Let cancellation handlers register grandchildren, then rescan once.
                await asyncio.sleep(0)
                protected_tasks = self._connection_runtime_tasks(context)
                newly_pending = {
                    task
                    for task in set(child_tasks)
                    if task is not current
                    and task not in protected_tasks
                    and not task.done()
                    and task not in cancelled_tasks
                }
                for task in newly_pending:
                    task.cancel()
                    cancelled_tasks.add(task)
                pending.update(newly_pending)
                if not newly_pending or remaining <= 0:
                    break
                continue
            await asyncio.wait(
                pending,
                timeout=min(0.05, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
        if pending:
            logger.warning("%d scraper background task(s) ignored cancellation", len(pending))
        cleanup = asyncio.create_task(context.close_extra_tabs())
        done, _ = await asyncio.wait({cleanup}, timeout=5)
        if not done:
            cleanup.cancel()
            logger.warning("Timed out closing scraper helper tabs")

    async def run(
        self,
        name: str,
        params: dict[str, Any],
        *,
        browser: Any,
        page: Any,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        path, source = self.store.read_bytes(name)
        code = _validate_source(source, str(path))
        revision = _revision(source)
        timeout = (
            self.settings.scraper_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if not 1 <= timeout <= self.settings.scraper_timeout_seconds:
            raise ScraperError(
                f"timeout_seconds must be between 1 and {self.settings.scraper_timeout_seconds}"
            )
        capture = _PrintCapture()
        module_name = f"_nodriver_scraper_{name}_{revision}_{uuid.uuid4().hex}"
        module = ModuleType(module_name)
        script_builtins = dict(vars(builtins))
        script_builtins["print"] = capture.print
        module.__dict__.update(
            {
                "__builtins__": script_builtins,
                "__file__": str(path),
                "__name__": module_name,
                "__package__": None,
            }
        )
        child_tasks: set[asyncio.Future[Any]] = set()
        loop = asyncio.get_running_loop()
        tracking_token = _SCRAPER_CHILD_TASKS.set(child_tasks)
        task_factory_lease = _install_task_tracking(loop, child_tasks)
        sys.modules[module_name] = module
        context = ScrapeContext(browser, page, self.settings, name)
        started = time.perf_counter()
        deadline: asyncio.Timeout | None = None
        try:
            try:
                exec(code, module.__dict__)
                entry_point = module.__dict__.get("scrape")
                if not inspect.iscoroutinefunction(entry_point):
                    raise ScraperError("`scrape` must be an async function")
                signature_parameters = list(inspect.signature(entry_point).parameters.values())
                positional_kinds = {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                }
                if len(signature_parameters) != 2 or any(
                    parameter.kind not in positional_kinds for parameter in signature_parameters
                ):
                    raise ScraperError("`scrape` must accept exactly (ctx, params)")
                remaining = timeout - (time.perf_counter() - started)
                if remaining <= 0:
                    raise TimeoutError
                deadline = asyncio.timeout(remaining)
                async with deadline:
                    result = await entry_point(context, params)
                if time.perf_counter() - started > timeout:
                    raise TimeoutError
            except asyncio.CancelledError:
                raise
            except ScraperError:
                raise
            except Exception as exc:
                timed_out = isinstance(exc, TimeoutError) and (
                    (deadline is not None and deadline.expired())
                    or time.perf_counter() - started >= timeout
                )
                if timed_out:
                    raise ScraperError(
                        f"Scraper exceeded the cooperative {timeout}-second timeout"
                    ) from exc
                formatted = "".join(
                    traceback.TracebackException.from_exception(exc, capture_locals=False).format()
                )
                lines = formatted.strip().splitlines()[-20:]
                raise ScraperError("Scraper failed:\n" + "\n".join(lines)) from exc

            try:
                encoded = await asyncio.to_thread(
                    lambda: json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
            except (TypeError, ValueError) as exc:
                raise ScraperError(f"Scraper result must be JSON-compatible: {exc}") from exc
            if len(encoded) > self.settings.max_artifact_result_bytes:
                raise ScraperError(
                    f"Scraper result is {len(encoded)} bytes; limit is "
                    f"{self.settings.max_artifact_result_bytes} bytes"
                )

            artifact = None
            inline_result = result
            if len(encoded) > self.settings.max_inline_result_bytes:
                artifact = await context._write_artifact_payload(
                    encoded,
                    label="result",
                    suffix="json",
                    output_format="json",
                )
                inline_result = None

            response: dict[str, Any] = {
                "ok": True,
                "scraper": name,
                "revision": revision,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "result": inline_result,
                "result_bytes": len(encoded),
                "artifact": artifact,
                "tab_id": tab_id(context.page),
            }
            if capture.value:
                response["output"] = capture.value
                response["output_truncated"] = capture.truncated
            return response
        finally:
            _SCRAPER_CHILD_TASKS.reset(tracking_token)
            cleanup = asyncio.create_task(self._cleanup_run(context, child_tasks))
            try:
                await self._await_cleanup_despite_cancellation(cleanup)
            finally:
                child_tasks.clear()
                _release_task_tracking(loop, task_factory_lease, child_tasks)
                sys.modules.pop(module_name, None)
