"""Persistent nodriver browser lifecycle and compact inspection helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import Context
from typing import Any
from urllib.parse import urlsplit

import nodriver

from nodriver_mcp.config import Settings

logger = logging.getLogger(__name__)

_background_cleanup_tasks: set[asyncio.Task[Any]] = set()
_FAILED_TARGET_CLEANUP_SECONDS = 5.0
_FAILED_START_CLEANUP_SECONDS = 20.0


class BrowserError(RuntimeError):
    """Raised when the managed browser cannot perform an operation."""


def validate_web_url(url: str) -> str:
    """Accept HTTP(S) pages and the one harmless blank browser URL."""
    value = url.strip()
    if value == "about:blank":
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserError("URL must be http://, https://, or exactly about:blank")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserError("URLs containing embedded credentials are not accepted")
    return value


def tab_id(tab: Any) -> str:
    target = getattr(tab, "target", None)
    identifier = getattr(target, "target_id", None)
    if identifier is None:
        raise BrowserError("nodriver returned a tab without a target id")
    return str(identifier)


def _target_id(identifier: Any) -> nodriver.cdp.target.TargetID:
    """Normalize test doubles and public string ids for typed CDP commands."""
    if isinstance(identifier, nodriver.cdp.target.TargetID):
        return identifier
    return nodriver.cdp.target.TargetID(str(identifier))


def _target_value(tab: Any, key: str, default: str = "") -> str:
    target = getattr(tab, "target", None)
    value = getattr(target, key, None)
    if value is None:
        value = getattr(tab, key, default)
    return str(value or default)


async def evaluate_json(tab: Any, expression: str) -> Any:
    """Evaluate a JSON-serializable expression while preserving falsy JS values."""
    wrapped = "(async () => JSON.stringify(await (\n" + expression + "\n)))()"
    raw = await tab.evaluate(wrapped, await_promise=True, return_by_value=True)
    if not isinstance(raw, str):
        raise BrowserError(f"JavaScript evaluation failed: {raw}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BrowserError("Page returned a non-JSON JavaScript result") from exc


class _NavigationTracker:
    """Track a Page.navigate loader and immediate main-frame redirect loaders."""

    def __init__(self, tab: Any) -> None:
        self.tab = tab
        self.frame_id: str | None = None
        self.loader_id: str | None = None
        self.events: list[tuple[str, str, str]] = []

    def install(self) -> None:
        self.tab.add_handler(nodriver.cdp.page.FrameStartedNavigating, self._on_started)
        self.tab.add_handler(nodriver.cdp.page.FrameNavigated, self._on_navigated)
        self.tab.add_handler(nodriver.cdp.page.LifecycleEvent, self._on_lifecycle)

    def remove(self) -> None:
        for event_type, callback in (
            (nodriver.cdp.page.FrameStartedNavigating, self._on_started),
            (nodriver.cdp.page.FrameNavigated, self._on_navigated),
            (nodriver.cdp.page.LifecycleEvent, self._on_lifecycle),
        ):
            with suppress(Exception):
                self.tab.remove_handler(event_type, callback)

    def bind(self, frame_id: Any, loader_id: Any) -> None:
        self.frame_id = str(frame_id)
        self.loader_id = str(loader_id)

    def _on_started(self, event: Any, *_: Any) -> None:
        self.events.append(("started", str(event.frame_id), str(event.loader_id)))

    def _on_navigated(self, event: Any, *_: Any) -> None:
        frame = event.frame
        if getattr(frame, "parent_id", None) is None:
            self.events.append(("committed", str(frame.id_), str(frame.loader_id)))

    def _on_lifecycle(self, event: Any, *_: Any) -> None:
        self.events.append(("lifecycle", str(event.frame_id), str(event.loader_id)))

    def accepts_loader(self, frame_id: Any, loader_id: Any) -> bool:
        """Return whether the current loader belongs to the requested navigation chain."""
        if self.frame_id is None or self.loader_id is None:
            return False
        frame = str(frame_id)
        loader = str(loader_id)
        if frame != self.frame_id:
            return False
        if loader == self.loader_id:
            return True

        allowed = {self.loader_id}
        committed = False
        for kind, event_frame, event_loader in self.events:
            if event_frame != self.frame_id:
                continue
            if event_loader in allowed:
                if kind in {"committed", "lifecycle"}:
                    committed = True
                continue
            if committed and kind in {"started", "committed"}:
                # A new loader beginning after a loader in this chain committed is an
                # immediate client/meta redirect of the same main frame.
                allowed.add(event_loader)
                if kind == "committed":
                    committed = True
        return loader in allowed


def _retain_background_cleanup(awaitable: Any) -> asyncio.Task[Any]:
    """Keep exact-target cleanup alive independently of caller cancellation."""
    task = _create_infrastructure_task(awaitable)
    _background_cleanup_tasks.add(task)

    def finished(completed: asyncio.Task[Any]) -> None:
        _background_cleanup_tasks.discard(completed)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.warning(
                "Deferred target cleanup failed: %s: %s",
                type(error).__name__,
                error,
            )

    task.add_done_callback(finished)
    return task


def _create_infrastructure_task(awaitable: Any) -> asyncio.Task[Any]:
    """Bypass scraper task tracking and its inherited ContextVar state."""
    return asyncio.Task(
        awaitable,
        loop=asyncio.get_running_loop(),
        context=Context(),
    )


async def _wait_for_cleanup_resisting_cancellation(
    cleanup_task: asyncio.Task[Any], *, timeout_seconds: float
) -> bool:
    """Let retained cleanup finish while coalescing repeated caller cancellation."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    current = asyncio.current_task()

    def clear_pending_cancellation() -> None:
        if current is None:
            return
        while current.cancelling():
            current.uncancel()

    clear_pending_cancellation()
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return cleanup_task.done()
        try:
            await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=remaining)
            return not cleanup_task.cancelled() and cleanup_task.exception() is None
        except asyncio.CancelledError:
            if cleanup_task.done() and cleanup_task.cancelled():
                return False
            clear_pending_cancellation()
        except TimeoutError:
            return cleanup_task.done()
        except Exception:
            return False


async def wait_for_page_ready(
    tab: Any,
    *,
    timeout_seconds: float,
    previous_document_marker: str | None = None,
    require_nonblank_url: bool = False,
    rejected_url: str | None = None,
    expected_loader_id: str | None = None,
    expected_frame_id: str | None = None,
    navigation_tracker: _NavigationTracker | None = None,
) -> None:
    """Wait for a navigation to commit and reach at least DOM interactive state."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error: Exception | None = None
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            detail = f" Last error: {last_error}" if last_error else ""
            raise BrowserError(f"Page did not become ready within {timeout_seconds}s.{detail}")
        try:
            if expected_loader_id is not None:
                frame_tree = await asyncio.wait_for(
                    tab.send(nodriver.cdp.page.get_frame_tree()),
                    timeout=remaining,
                )
                current_frame = getattr(frame_tree, "frame", None)
                current_frame_id = getattr(current_frame, "id_", expected_frame_id)
                current_loader_id = getattr(current_frame, "loader_id", None)
                loader_matches = str(current_loader_id or "") == expected_loader_id
                if navigation_tracker is not None:
                    loader_matches = navigation_tracker.accepts_loader(
                        current_frame_id, current_loader_id
                    )
                if not loader_matches:
                    await asyncio.sleep(min(0.05, max(remaining, 0)))
                    continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
            state = await asyncio.wait_for(
                evaluate_json(
                    tab,
                    "({"
                    "url: location.href,"
                    "ready: document.readyState || 'loading',"
                    "oldDocument: "
                    + (
                        f"globalThis[{json.dumps(previous_document_marker)}] === true"
                        if previous_document_marker
                        else "false"
                    )
                    + "})",
                ),
                timeout=remaining,
            )
            committed_url = str(state.get("url") or "")
            is_transient_blank = committed_url in {
                "",
                "about:blank",
                "chrome://newtab/",
                "chrome://new-tab-page/",
            }
            if (
                not state.get("oldDocument")
                and (not require_nonblank_url or not is_transient_blank)
                and (rejected_url is None or committed_url.rstrip("/") != rejected_url.rstrip("/"))
                and state.get("ready")
                in {
                    "interactive",
                    "complete",
                }
            ):
                return
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(min(0.05, max(remaining, 0)))


async def navigate_page(tab: Any, url: str, *, timeout_seconds: float) -> Any:
    """Navigate an attached tab without creating another CDP target session."""
    safe_url = validate_web_url(url)
    previous_url = _target_value(tab, "url")
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    marker = f"__nodriver_mcp_document_{uuid.uuid4().hex}"
    marker_json = json.dumps(marker)
    marker_was_set = False
    try:
        marker_was_set = bool(
            await asyncio.wait_for(
                evaluate_json(
                    tab,
                    f"(() => {{ globalThis[{marker_json}] = true; return true; }})()",
                ),
                timeout=min(2, max(deadline - asyncio.get_running_loop().time(), 0.001)),
            )
        )
    except Exception:
        # A newly attached or recovering target may not have an evaluable old document.
        marker_was_set = False

    tracker: _NavigationTracker | None = None
    if hasattr(tab, "add_handler") and hasattr(tab, "remove_handler"):
        candidate = _NavigationTracker(tab)
        try:
            candidate.install()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(tab.send(nodriver.cdp.page.enable()), timeout=remaining)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(
                tab.send(nodriver.cdp.page.set_lifecycle_events_enabled(True)),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            candidate.remove()
            raise
        except Exception:
            candidate.remove()
        else:
            tracker = candidate

    try:
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            response = await asyncio.wait_for(
                tab.send(nodriver.cdp.page.navigate(safe_url)), timeout=remaining
            )
        except TimeoutError as exc:
            raise BrowserError(f"Navigation did not start within {timeout_seconds}s") from exc
        except Exception as exc:
            raise BrowserError(f"Navigation failed: {type(exc).__name__}: {exc}") from exc

        frame_id = response[0] if isinstance(response, tuple) and response else None
        loader_id = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        error_text = response[2] if isinstance(response, tuple) and len(response) > 2 else None
        is_download = response[3] if isinstance(response, tuple) and len(response) > 3 else False
        if error_text:
            raise BrowserError(f"Navigation was rejected by the browser: {error_text}")
        if is_download:
            raise BrowserError("Navigation started a download instead of opening a page")
        bound_tracker = (
            tracker
            if tracker is not None and frame_id is not None and loader_id is not None
            else None
        )
        if bound_tracker is not None:
            bound_tracker.bind(frame_id, loader_id)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise BrowserError(f"Page did not become ready within {timeout_seconds}s")
        await wait_for_page_ready(
            tab,
            timeout_seconds=remaining,
            previous_document_marker=marker if marker_was_set and loader_id is not None else None,
            require_nonblank_url=safe_url != "about:blank",
            rejected_url=(
                previous_url
                if not marker_was_set
                and bound_tracker is None
                and previous_url.rstrip("/") != safe_url.rstrip("/")
                else None
            ),
            expected_loader_id=str(loader_id) if bound_tracker is not None else None,
            expected_frame_id=str(frame_id) if bound_tracker is not None else None,
            navigation_tracker=bound_tracker,
        )
        return tab
    finally:
        if tracker is not None:
            tracker.remove()


async def create_new_page(
    browser: Any,
    url: str,
    *,
    timeout_seconds: float,
    on_target_created: Callable[[str], None] | None = None,
) -> Any:
    """Create, attach, and ready a new target while retaining cleanup ownership."""
    safe_url = validate_web_url(url)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    created_id: nodriver.cdp.target.TargetID | None = None
    page: Any | None = None

    async def issue_create() -> nodriver.cdp.target.TargetID:
        nonlocal created_id
        identifier = await browser.send(nodriver.cdp.target.create_target(safe_url))
        created_id = _target_id(identifier)
        if on_target_created is not None:
            on_target_created(str(created_id))
        return created_id

    create_task = _create_infrastructure_task(issue_create())

    async def cleanup_failed_target() -> None:
        nonlocal created_id
        try:
            async with asyncio.timeout(_FAILED_TARGET_CLEANUP_SECONDS):
                if created_id is None:
                    try:
                        created_id = await create_task
                    except BaseException:
                        return
                identifier = created_id
                retained_page = page
                if retained_page is None:
                    retained_page = next(
                        (
                            candidate
                            for candidate in list(getattr(browser, "tabs", []))
                            if tab_id(candidate) == str(identifier)
                        ),
                        None,
                    )
                await _run_cleanup_step(
                    browser.send(nodriver.cdp.target.close_target(identifier)),
                    timeout_seconds=2,
                )
                if retained_page is not None and hasattr(retained_page, "aclose"):
                    await _run_cleanup_step(retained_page.aclose(), timeout_seconds=2)
                with suppress(Exception):
                    await asyncio.wait_for(browser.update_targets(), timeout=2)
        except TimeoutError:
            logger.warning(
                "Exact-target cleanup exceeded %.1f seconds",
                _FAILED_TARGET_CLEANUP_SECONDS,
            )
        finally:
            if not create_task.done():
                create_task.cancel()
            with suppress(BaseException):
                await create_task

    cleanup_task: asyncio.Task[Any] | None = None

    def retain_cleanup() -> asyncio.Task[Any]:
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = _retain_background_cleanup(cleanup_failed_target())
        return cleanup_task

    try:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        target_identifier = await asyncio.wait_for(
            asyncio.shield(create_task),
            timeout=remaining,
        )

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(browser.update_targets(), timeout=remaining)
        page = next(
            (
                candidate
                for candidate in list(getattr(browser, "tabs", []))
                if tab_id(candidate) == str(target_identifier)
            ),
            None,
        )
        if page is None:
            raise BrowserError("The browser created a target but did not expose its page")

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(page.attach(), timeout=remaining)

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await wait_for_page_ready(
            page,
            timeout_seconds=remaining,
            require_nonblank_url=safe_url != "about:blank",
        )
        return page
    except asyncio.CancelledError:
        await _wait_for_cleanup_resisting_cancellation(
            retain_cleanup(),
            timeout_seconds=_FAILED_TARGET_CLEANUP_SECONDS + 0.5,
        )
        raise
    except TimeoutError as exc:
        await _run_cleanup_step(asyncio.shield(retain_cleanup()), timeout_seconds=5)
        raise BrowserError(f"New page did not become ready within {timeout_seconds}s") from exc
    except Exception:
        await _run_cleanup_step(asyncio.shield(retain_cleanup()), timeout_seconds=5)
        raise


def _connection_appears_alive(connection: Any, *, allow_unattached: bool = False) -> bool:
    if not hasattr(connection, "socket"):
        return True  # lightweight test doubles
    socket = getattr(connection, "socket", None)
    if socket is None:
        return allow_unattached and getattr(connection, "session_id", None) is None
    if getattr(socket, "close_code", None) is not None:
        return False
    listener = getattr(connection, "_listener_task", None)
    return listener is None or not listener.done()


async def _run_cleanup_step(awaitable: Any, *, timeout_seconds: float) -> bool:
    """Run best-effort cleanup with a hard deadline even if cancellation is ignored."""
    task = asyncio.ensure_future(awaitable)

    def drain_result(completed: asyncio.Future[Any]) -> None:
        with suppress(BaseException):
            completed.exception()

    task.add_done_callback(drain_result)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except BaseException:
        if not task.done():
            task.cancel()
        raise
    if not done:
        task.cancel()
        return False
    return not task.cancelled() and task.exception() is None


async def close_page_connection(tab: Any, *, timeout_seconds: float = 2) -> tuple[bool, bool]:
    """Close a target and its per-tab WebSocket without allowing cleanup to hang."""
    target_closed = True
    if hasattr(tab, "close"):
        target_closed = await _run_cleanup_step(tab.close(), timeout_seconds=timeout_seconds)
    connection_closed = True
    if hasattr(tab, "aclose"):
        connection_closed = await _run_cleanup_step(tab.aclose(), timeout_seconds=timeout_seconds)
    return target_closed, connection_closed


async def close_page_with_browser(browser: Any, tab: Any, *, timeout_seconds: float = 2) -> bool:
    """Close a page connection, falling back to the root browser target session."""
    identifier = tab_id(tab)
    target_closed, connection_closed = await close_page_connection(
        tab, timeout_seconds=timeout_seconds
    )
    if not target_closed:

        async def close_from_root() -> None:
            result = await browser.send(nodriver.cdp.target.close_target(_target_id(identifier)))
            if result is False:
                raise BrowserError("The root browser did not close the target")

        target_closed = await _run_cleanup_step(close_from_root(), timeout_seconds=timeout_seconds)
    if not target_closed and hasattr(browser, "update_targets"):
        refreshed = await _run_cleanup_step(
            browser.update_targets(), timeout_seconds=timeout_seconds
        )
        if refreshed:
            target_closed = identifier not in {
                tab_id(page) for page in list(getattr(browser, "tabs", []))
            }
    return target_closed and connection_closed


class BrowserManager:
    """Own one lazy browser and serialize operations that share its profile."""

    def __init__(
        self,
        settings: Settings,
        *,
        starter: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self._starter = starter or nodriver.start
        self._custom_starter = starter is not None
        self._browser: Any | None = None
        self._running_headless: bool | None = None
        self._active_tab_id: str | None = None
        self._owned_tab_ids: set[str] = set()
        self._owned_tab_connections: dict[str, Any] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._owns_browser = not settings.attach_mode

    @property
    def browser(self) -> Any | None:
        return self._browser

    @property
    def busy(self) -> bool:
        return self._operation_lock.locked()

    def _appears_alive(self) -> bool:
        if self._browser is None:
            return False
        if self._owns_browser and bool(getattr(self._browser, "stopped", False)):
            return False
        return _connection_appears_alive(self._browser)

    async def _direct_websocket_attach(self) -> Any:
        url = self.settings.connect_websocket_url
        if url is None:
            raise BrowserError("Direct WebSocket attach URL is not configured")
        parsed = urlsplit(url)
        config = nodriver.Config(
            user_data_dir=self.settings.user_data_dir,
            host=parsed.hostname,
            port=parsed.port,
            browser_executable_path=self.settings.browser_executable_path,
        )
        browser = nodriver.Browser(config)
        browser.websocket_url = url
        try:
            await browser.attach()
            await browser.update_targets()
        except BaseException:
            await self._await_failed_start_cleanup(browser)
            raise
        return browser

    async def _start_configured_browser(self, *, headless: bool | None) -> Any:
        if self._custom_starter:
            if self.settings.connect_websocket_url:
                return await self._starter(websocket_url=self.settings.connect_websocket_url)
            if self.settings.attach_mode:
                return await self._starter(
                    host=self.settings.connect_host,
                    port=self.settings.connect_port,
                )
            return await self._starter(**self._managed_start_arguments(headless=headless))
        if self.settings.connect_websocket_url:
            return await self._direct_websocket_attach()
        if self.settings.attach_mode:
            config = nodriver.Config(
                user_data_dir=self.settings.user_data_dir,
                host=self.settings.connect_host,
                port=self.settings.connect_port,
                browser_executable_path=self.settings.browser_executable_path,
            )
            browser = nodriver.Browser(config)
            try:
                await browser.start()
            except BaseException:
                await self._await_failed_start_cleanup(browser)
                raise
            return browser
        self.settings.user_data_dir.mkdir(parents=True, exist_ok=True)
        config = nodriver.Config(**self._managed_start_arguments(headless=headless))
        browser = nodriver.Browser(config)
        try:
            await browser.start()
        except BaseException:
            await self._await_failed_start_cleanup(browser)
            raise
        return browser

    def _managed_start_arguments(self, *, headless: bool | None) -> dict[str, Any]:
        if headless is None:
            raise BrowserError("Managed browser startup requires a headless mode")
        browser_args = list(self.settings.browser_args)
        if not any(arg.startswith("--profile-directory=") for arg in browser_args):
            browser_args.append(f"--profile-directory={self.settings.profile_directory}")
        return {
            "user_data_dir": self.settings.user_data_dir,
            "headless": headless,
            "browser_executable_path": self.settings.browser_executable_path,
            "browser_args": browser_args,
            "sandbox": True,
            "lang": self.settings.language,
            "expert": False,
        }

    async def _cleanup_failed_start(self, browser: Any) -> None:
        try:
            if self._owns_browser:
                await self._close_managed(browser)
            elif hasattr(browser, "aclose"):
                await _run_cleanup_step(browser.aclose(), timeout_seconds=5)
        except BaseException:
            logger.warning("Could not completely clean up a failed browser start", exc_info=True)

    async def _await_failed_start_cleanup(self, browser: Any) -> None:
        """Keep partial-start cleanup alive across repeated caller cancellation."""
        cleanup = _retain_background_cleanup(self._cleanup_failed_start(browser))
        finished = await _wait_for_cleanup_resisting_cancellation(
            cleanup,
            timeout_seconds=_FAILED_START_CLEANUP_SECONDS,
        )
        if not finished:
            logger.warning(
                "Failed browser-start cleanup exceeded %.1f seconds and remains retained",
                _FAILED_START_CLEANUP_SECONDS,
            )

    async def ensure_started(
        self,
        *,
        force_restart: bool = False,
        headless: bool | None = None,
    ) -> Any:
        """Start or reuse the browser, optionally selecting managed headless mode.

        An omitted mode inherits a running managed browser and otherwise uses the
        configured default. An explicit mode change restarts a managed browser.
        """
        if self.settings.attach_mode and headless is not None:
            raise BrowserError(
                "Headless mode cannot be changed while attaching to an externally managed "
                "browser; launch that browser in the desired mode and omit headless"
            )
        requested_headless = (
            None
            if self.settings.attach_mode
            else (
                headless
                if headless is not None
                else self._running_headless
                if self._running_headless is not None
                else self.settings.headless
            )
        )
        if (
            not force_restart
            and self._appears_alive()
            and (headless is None or self._running_headless == requested_headless)
        ):
            return self._browser
        async with self._lifecycle_lock:
            if (
                not force_restart
                and self._appears_alive()
                and (headless is None or self._running_headless == requested_headless)
            ):
                return self._browser
            stale = self._browser
            changing_mode = (
                stale is not None
                and self._appears_alive()
                and headless is not None
                and self._running_headless != requested_headless
            )
            if stale is not None:
                try:
                    if self._owns_browser:
                        await self._close_managed(stale)
                    else:
                        await self._close_attached(stale)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = (
                        "The browser could not be restarted to change headless mode"
                        if changing_mode
                        else "The previous browser connection died and its process could not "
                        "be cleaned up"
                    )
                    raise BrowserError(f"{reason}: {type(exc).__name__}: {exc}") from exc
                self._browser = None
                self._running_headless = None
                self._active_tab_id = None
                self._owned_tab_ids.clear()
                self._owned_tab_connections.clear()
            try:
                browser = await asyncio.wait_for(
                    self._start_configured_browser(headless=requested_headless),
                    timeout=self.settings.browser_start_timeout_seconds,
                )
            except TimeoutError as exc:
                mode = "attachment" if self.settings.attach_mode else "browser startup"
                raise BrowserError(
                    f"{mode.capitalize()} exceeded "
                    f"{self.settings.browser_start_timeout_seconds} seconds"
                ) from exc
            except Exception as exc:
                mode = "attach to" if self.settings.attach_mode else "start"
                raise BrowserError(
                    f"Could not {mode} Helium/Chromium. If it is a managed profile, close any "
                    "other browser using that dedicated profile and try again. "
                    f"Underlying error: {type(exc).__name__}: {exc}"
                ) from exc
            self._browser = browser
            self._running_headless = requested_headless
            self._owned_tab_ids.clear()
            self._owned_tab_connections.clear()
            self._active_tab_id = None
            tabs = list(getattr(browser, "tabs", []))
            if tabs and self._owns_browser:
                self._active_tab_id = tab_id(tabs[0])
                self._owned_tab_ids.add(self._active_tab_id)
                self._owned_tab_connections[self._active_tab_id] = tabs[0]
            return browser

    async def _refresh_targets(self, browser: Any) -> None:
        """Refresh target membership and close connections for removed owned tabs."""
        if not hasattr(browser, "update_targets"):
            return  # lightweight test doubles
        previous = {tab_id(page): page for page in list(getattr(browser, "tabs", []))}
        try:
            await asyncio.wait_for(
                browser.update_targets(),
                timeout=min(5, self.settings.navigation_timeout_seconds),
            )
        except TimeoutError as exc:
            raise BrowserError("Timed out refreshing browser tabs") from exc
        except Exception as exc:
            raise BrowserError(
                f"Could not refresh browser tabs: {type(exc).__name__}: {exc}"
            ) from exc
        current_ids = {tab_id(page) for page in list(getattr(browser, "tabs", []))}
        removed_owned = [
            page
            for identifier, page in previous.items()
            if identifier in self._owned_tab_ids and identifier not in current_ids
        ]
        if removed_owned:
            results = await asyncio.gather(
                *(close_page_connection(page) for page in removed_owned),
                return_exceptions=True,
            )
            for page, result in zip(removed_owned, results, strict=True):
                if isinstance(result, tuple) and len(result) == 2 and result[1] is True:
                    self._owned_tab_connections.pop(tab_id(page), None)
        self._owned_tab_ids.intersection_update(current_ids)

    @asynccontextmanager
    async def operation(self, *, headless: bool | None = None) -> AsyncIterator[Any]:
        """Serialize access to nodriver's shared target/profile state."""
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._operation_lock.acquire(),
                    timeout=self.settings.operation_queue_timeout_seconds,
                )
                acquired = True
            except TimeoutError as exc:
                raise BrowserError(
                    "Browser is busy with another scraper or navigation; try again shortly"
                ) from exc
            try:
                deadline = asyncio.timeout(self.settings.operation_timeout_seconds)
                async with deadline:
                    browser = await self.ensure_started(headless=headless)
                    try:
                        await self._refresh_targets(browser)
                    except BrowserError:
                        browser = await self.ensure_started(
                            force_restart=True,
                            headless=headless,
                        )
                        await self._refresh_targets(browser)
                    yield browser
            except TimeoutError as exc:
                if deadline.expired():
                    raise BrowserError(
                        "Browser operation exceeded "
                        f"{self.settings.operation_timeout_seconds} seconds and was cancelled"
                    ) from exc
                raise
        finally:
            if acquired:
                self._operation_lock.release()

    def _accessible_tabs(self, browser: Any) -> list[Any]:
        tabs = list(getattr(browser, "tabs", []))
        current_ids = {tab_id(page) for page in tabs}
        self._owned_tab_ids.intersection_update(current_ids)
        candidates = (
            tabs
            if self._owns_browser
            else [page for page in tabs if tab_id(page) in self._owned_tab_ids]
        )
        return [
            page for page in candidates if _connection_appears_alive(page, allow_unattached=True)
        ]

    def resolve_tab(self, browser: Any, requested_id: str | None = None) -> Any:
        tabs = self._accessible_tabs(browser)
        if not tabs:
            detail = " managed by this MCP" if not self._owns_browser else ""
            raise BrowserError(f"The browser has no open page tabs{detail}; call browser_open")
        wanted = requested_id or self._active_tab_id
        if wanted:
            exact = [tab for tab in tabs if tab_id(tab) == wanted]
            if exact:
                return exact[0]
            prefixes = [tab for tab in tabs if tab_id(tab).startswith(wanted)]
            if len(prefixes) == 1:
                return prefixes[0]
            if requested_id:
                raise BrowserError(f"No open tab has id {requested_id!r}")
            if self._active_tab_id is not None:
                raise BrowserError(
                    "The active tab was closed; call browser_open to create a replacement "
                    "or provide another tab_id"
                )
        self._active_tab_id = tab_id(tabs[0])
        return tabs[0]

    async def open(self, browser: Any, url: str, *, new_tab: bool = False) -> Any:
        safe_url = validate_web_url(url)
        accessible_tabs = self._accessible_tabs(browser)
        if self._active_tab_id is not None and not any(
            tab_id(page) == self._active_tab_id for page in accessible_tabs
        ):
            # Never silently navigate a different owned tab after the active one closed.
            accessible_tabs = []
        if safe_url == "about:blank" and accessible_tabs and not new_tab:
            page = self.resolve_tab(browser)
            return page
        elif new_tab or not accessible_tabs:

            def own_target(identifier: str) -> None:
                self._active_tab_id = identifier
                self._owned_tab_ids.add(identifier)

            page = await create_new_page(
                browser,
                safe_url,
                timeout_seconds=self.settings.navigation_timeout_seconds,
                on_target_created=own_target,
            )
        else:
            page = self.resolve_tab(browser)
            page = await navigate_page(
                page,
                safe_url,
                timeout_seconds=self.settings.navigation_timeout_seconds,
            )
        self._active_tab_id = tab_id(page)
        self._owned_tab_ids.add(self._active_tab_id)
        self._owned_tab_connections[self._active_tab_id] = page
        return page

    async def close(self) -> bool:
        try:
            await asyncio.wait_for(
                self._operation_lock.acquire(),
                timeout=self.settings.operation_queue_timeout_seconds,
            )
        except TimeoutError as exc:
            raise BrowserError("Browser is busy and could not be closed safely") from exc
        try:
            async with self._lifecycle_lock:
                browser = self._browser
                if browser is None:
                    return False
                try:
                    if self._owns_browser:
                        await self._close_managed(browser)
                    else:
                        await self._close_attached(browser)
                except Exception as exc:
                    raise BrowserError(
                        f"Browser shutdown failed: {type(exc).__name__}: {exc}"
                    ) from exc
                self._browser = None
                self._running_headless = None
                self._active_tab_id = None
                self._owned_tab_ids.clear()
                self._owned_tab_connections.clear()
                return True
        finally:
            self._operation_lock.release()

    async def _close_managed(self, browser: Any) -> None:
        process = getattr(browser, "_process", None)
        with suppress(Exception):
            await asyncio.wait_for(browser.send(nodriver.cdp.browser.close()), timeout=3)
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        process.kill()
                    await asyncio.wait_for(process.wait(), timeout=3)
        if hasattr(browser, "aclose") and not await _run_cleanup_step(
            browser.aclose(), timeout_seconds=2
        ):
            raise BrowserError("Timed out closing the browser connection")

    async def _close_attached(self, browser: Any) -> None:
        owned_by_id = dict(self._owned_tab_connections)
        owned_by_id.update(
            {
                tab_id(page): page
                for page in list(getattr(browser, "tabs", []))
                if tab_id(page) in self._owned_tab_ids
            }
        )
        owned_pages = list(owned_by_id.values())
        results = await asyncio.gather(
            *(close_page_with_browser(browser, page) for page in reversed(owned_pages)),
            return_exceptions=True,
        )
        failures = [result for result in results if result is not True]
        if failures:
            raise BrowserError(f"Could not close {len(failures)} MCP-owned attached tab(s)")
        if not await _run_cleanup_step(browser.aclose(), timeout_seconds=5):
            raise BrowserError("Timed out detaching from the externally managed browser")

    async def status(self) -> dict[str, Any]:
        browser = self._browser
        running = self._appears_alive()
        tabs: list[dict[str, Any]] = []
        unmanaged_tab_count = 0
        if running and browser is not None:
            all_tabs = list(getattr(browser, "tabs", []))
            accessible_tabs = self._accessible_tabs(browser)
            unmanaged_tab_count = sum(
                1 for page in all_tabs if tab_id(page) not in self._owned_tab_ids
            )
            for page in accessible_tabs:
                identifier = tab_id(page)
                tabs.append(
                    {
                        "id": identifier,
                        "active": identifier == self._active_tab_id,
                        "title": _target_value(page, "title"),
                        "url": _target_value(page, "url", "about:blank"),
                    }
                )
        configuration = self.settings.public_browser_config()
        configuration["default_headless"] = configuration.pop("headless")
        return {
            "running": running,
            "running_headless": (
                self._running_headless if running and self._owns_browser else None
            ),
            "busy": self.busy,
            "tabs": tabs,
            "unmanaged_tab_count": unmanaged_tab_count,
            "configuration": configuration,
        }


async def page_summary(tab: Any, *, text_chars: int = 2_000) -> dict[str, Any]:
    expression = """({
        title: document.title || '',
        url: location.href,
        text: ((document.body && document.body.innerText) || '').slice(0, __TEXT_CHARS__)
    })""".replace("__TEXT_CHARS__", str(text_chars))
    data = await evaluate_json(
        tab,
        expression,
    )
    return {"tab_id": tab_id(tab), **data}


async def page_snapshot(
    tab: Any,
    *,
    output_format: str = "text",
    selector: str | None = None,
    max_chars: int = 30_000,
) -> dict[str, Any]:
    if output_format not in {"text", "html"}:
        raise BrowserError("format must be 'text' or 'html'")
    if not 1_000 <= max_chars <= 200_000:
        raise BrowserError("max_chars must be between 1,000 and 200,000")
    selector_json = json.dumps(selector) if selector else "null"
    value_key = "innerText" if output_format == "text" else "outerHTML"
    expression = """(() => {
        const selector = __SELECTOR__;
        const node = selector ? document.querySelector(selector) :
            (__VALUE_KEY__ === 'innerText' ? document.body : document.documentElement);
        if (!node) return {found: false, content: '', total_chars: 0};
        const full = String(node[__VALUE_KEY__] || '');
        return {
            found: true,
            content: full.slice(0, __MAX_CHARS__),
            total_chars: full.length,
            truncated: full.length > __MAX_CHARS__
        };
    })()"""
    expression = (
        expression.replace("__SELECTOR__", selector_json)
        .replace("__VALUE_KEY__", json.dumps(value_key))
        .replace("__MAX_CHARS__", str(max_chars))
    )
    data = await evaluate_json(tab, expression)
    return {
        "tab_id": tab_id(tab),
        "url": _target_value(tab, "url"),
        "format": output_format,
        "selector": selector,
        **data,
    }


async def query_page(tab: Any, selector: str, *, limit: int = 20) -> dict[str, Any]:
    if not selector.strip():
        raise BrowserError("selector cannot be empty")
    if not 1 <= limit <= 100:
        raise BrowserError("limit must be between 1 and 100")
    expression = """(() => {
        const selector = __SELECTOR__;
        const limit = __LIMIT__;
        const safeAttributes = new Set([
            'id', 'class', 'href', 'src', 'alt', 'title', 'role', 'name', 'type',
            'aria-label', 'aria-labelledby', 'aria-describedby'
        ]);
        const all = Array.from(document.querySelectorAll(selector));
        return {
            total: all.length,
            truncated: all.length > limit,
            matches: all.slice(0, limit).map((element, index) => ({
                index,
                tag: element.tagName.toLowerCase(),
                text: String(element.innerText || element.textContent || '').trim().slice(0, 1000),
                attributes: Object.fromEntries(
                    Array.from(element.attributes)
                        .filter(attr => safeAttributes.has(attr.name))
                        .map(attr => [attr.name, attr.value.slice(0, 2000)])
                )
            }))
        };
    })()"""
    expression = expression.replace("__SELECTOR__", json.dumps(selector)).replace(
        "__LIMIT__", str(limit)
    )
    data = await evaluate_json(tab, expression)
    return {"tab_id": tab_id(tab), "selector": selector, **data}
