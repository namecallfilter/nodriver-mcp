from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

import nodriver_mcp.browser as browser_module
from nodriver_mcp.browser import (
    BrowserError,
    BrowserManager,
    create_new_page,
    evaluate_json,
    navigate_page,
)


@dataclass
class FakeTarget:
    target_id: str
    url: str = "about:blank"
    title: str = ""


class FakePage:
    def __init__(self, identifier: str, url: str = "about:blank") -> None:
        self.target = FakeTarget(identifier, url, identifier)
        self.closed = False
        self.aclose_called = False
        self.navigations: list[str] = []
        self.expressions: list[str] = []

    async def get(self, url: str) -> FakePage:
        self.navigations.append(url)
        self.target.url = url
        return self

    async def send(self, command: Any) -> tuple[None, str, None, None]:
        payload = next(command)
        if payload["method"] == "Page.getFrameTree":
            return SimpleNamespace(frame=SimpleNamespace(loader_id="loader"))  # type: ignore[return-value]
        assert payload["method"] == "Page.navigate"
        url = payload["params"]["url"]
        self.navigations.append(url)
        self.target.url = url
        return None, "loader", None, None

    async def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool,
        return_by_value: bool,
    ) -> str:
        assert await_promise is True
        assert return_by_value is True
        self.expressions.append(expression)
        return json.dumps({"url": self.target.url, "ready": "complete"})

    async def close(self) -> None:
        self.closed = True

    async def attach(self) -> None:
        return None

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeBrowser:
    def __init__(self, tabs: list[FakePage] | None = None) -> None:
        self.tabs = list(tabs or [])
        self.get_calls: list[tuple[str, bool]] = []
        self.aclose_called = False
        self.stop_called = False
        self.stopped = False

    async def get(self, url: str, *, new_tab: bool) -> FakePage:
        self.get_calls.append((url, new_tab))
        page = FakePage(f"mcp-{len(self.get_calls)}", url)
        self.tabs.append(page)
        return page

    async def send(self, command: Any) -> Any:
        payload = next(command)
        if payload["method"] == "Target.createTarget":
            url = payload["params"]["url"]
            self.get_calls.append((url, True))
            page = FakePage(f"mcp-{len(self.get_calls)}", url)
            self.tabs.append(page)
            return page.target.target_id
        if payload["method"] == "Target.closeTarget":
            identifier = str(payload["params"]["targetId"])
            for page in self.tabs:
                if str(page.target.target_id) == identifier:
                    page.closed = True
            self.tabs = [page for page in self.tabs if str(page.target.target_id) != identifier]
            return True
        if payload["method"] == "Browser.close":
            return None
        raise AssertionError(payload["method"])

    async def update_targets(self) -> None:
        return None

    async def aclose(self) -> None:
        self.aclose_called = True

    def stop(self) -> None:
        self.stop_called = True


@pytest.mark.asyncio
async def test_evaluate_json_awaits_async_expression_before_stringifying() -> None:
    page = FakePage("page")
    page.target.url = "https://example.com"

    result = await evaluate_json(page, "Promise.resolve({value: 42})")

    assert result == {"url": "https://example.com", "ready": "complete"}
    assert "JSON.stringify(await (" in page.expressions[0]
    assert "Promise.resolve({value: 42})" in page.expressions[0]


@pytest.mark.asyncio
async def test_concurrent_start_only_invokes_browser_starter_once(settings_factory) -> None:
    browser = FakeBrowser([FakePage("initial")])
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def starter(**kwargs: Any) -> FakeBrowser:
        nonlocal calls
        del kwargs
        calls += 1
        entered.set()
        await release.wait()
        return browser

    manager = BrowserManager(settings_factory(), starter=starter)
    first = asyncio.create_task(manager.ensure_started())
    await entered.wait()
    second = asyncio.create_task(manager.ensure_started())
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [browser, browser]
    assert calls == 1


@pytest.mark.asyncio
async def test_explicit_headless_mode_restarts_managed_browser_and_omission_inherits(
    settings_factory,
) -> None:
    starts: list[dict[str, Any]] = []
    browsers: list[FakeBrowser] = []

    async def starter(**kwargs: Any) -> FakeBrowser:
        starts.append(kwargs)
        browser = FakeBrowser([FakePage(f"initial-{len(starts)}")])
        browsers.append(browser)
        return browser

    manager = BrowserManager(settings_factory(headless=False), starter=starter)

    async with manager.operation(headless=False) as visible:
        assert visible is browsers[0]
    async with manager.operation(headless=True) as hidden:
        assert hidden is browsers[1]
    async with manager.operation() as inherited:
        assert inherited is hidden

    status = await manager.status()
    assert [call["headless"] for call in starts] == [False, True]
    assert browsers[0].aclose_called is True
    assert browsers[1].aclose_called is False
    assert status["running_headless"] is True
    assert status["configuration"]["default_headless"] is False
    assert "headless" not in status["configuration"]

    async with manager.operation(headless=False) as visible_again:
        assert visible_again is browsers[2]

    assert [call["headless"] for call in starts] == [False, True, False]
    assert browsers[1].aclose_called is True


@pytest.mark.asyncio
async def test_configured_headless_default_applies_only_to_first_managed_start(
    settings_factory,
) -> None:
    starts: list[dict[str, Any]] = []
    browser = FakeBrowser([FakePage("initial")])

    async def starter(**kwargs: Any) -> FakeBrowser:
        starts.append(kwargs)
        return browser

    manager = BrowserManager(settings_factory(headless=True), starter=starter)

    assert await manager.ensure_started() is browser
    assert starts[0]["headless"] is True
    assert (await manager.status())["running_headless"] is True


@pytest.mark.asyncio
async def test_attach_mode_rejects_explicit_headless_override_before_connecting(
    settings_factory,
) -> None:
    starts: list[dict[str, Any]] = []

    async def starter(**kwargs: Any) -> FakeBrowser:
        starts.append(kwargs)
        return FakeBrowser()

    manager = BrowserManager(
        settings_factory(connect_host="127.0.0.1", connect_port=9222),
        starter=starter,
    )

    with pytest.raises(BrowserError, match="cannot be changed.*externally managed"):
        await manager.ensure_started(headless=True)

    assert starts == []
    assert await manager.ensure_started() is not None
    assert starts == [{"host": "127.0.0.1", "port": 9222}]
    status = await manager.status()
    assert status["running_headless"] is None
    assert status["configuration"]["default_headless"] is False


@pytest.mark.asyncio
async def test_dead_managed_connection_closes_process_before_restart(settings_factory) -> None:
    events: list[str] = []

    class ClosedSocket:
        close_code = 1006

    class GracefulProcess:
        returncode = None

        async def wait(self) -> None:
            events.append("process-wait")
            self.returncode = 0

    class StaleBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__([FakePage("stale")])
            self.socket = ClosedSocket()
            self._process = GracefulProcess()

        async def send(self, command: Any) -> None:
            del command
            events.append("browser-close")

        async def aclose(self) -> None:
            events.append("connection-close")

    replacement = FakeBrowser([FakePage("replacement")])

    async def starter(**kwargs: Any) -> FakeBrowser:
        del kwargs
        events.append("restart")
        return replacement

    manager = BrowserManager(settings_factory(), starter=starter)
    manager._browser = StaleBrowser()

    assert await manager.ensure_started() is replacement
    assert events == ["browser-close", "process-wait", "connection-close", "restart"]


@pytest.mark.asyncio
async def test_managed_start_timeout_cleans_partial_process(
    settings_factory, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class PartialProcess:
        returncode = None

        def __init__(self) -> None:
            self.terminated = False

        async def wait(self) -> None:
            if not self.terminated:
                raise TimeoutError
            self.returncode = 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient")

    class PartialBrowser:
        def __init__(self, config: Any) -> None:
            del config
            self._process = PartialProcess()
            self.aclose_called = False

        async def start(self) -> None:
            await asyncio.sleep(60)

        async def send(self, command: Any) -> None:
            del command
            raise RuntimeError("not attached yet")

        async def aclose(self) -> None:
            self.aclose_called = True

    partial: PartialBrowser | None = None

    def browser_factory(config: Any) -> PartialBrowser:
        nonlocal partial
        partial = PartialBrowser(config)
        return partial

    monkeypatch.setattr(browser_module.nodriver, "Browser", browser_factory)
    manager = BrowserManager(
        settings_factory(
            browser_executable_path=tmp_path / "helium.exe",
            browser_start_timeout_seconds=0.01,
        )
    )

    with pytest.raises(BrowserError, match="startup exceeded"):
        await manager.ensure_started()

    assert partial is not None
    assert partial._process.terminated is True
    assert partial.aclose_called is True


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_partial_start_cleanup(
    settings_factory, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    class PartialProcess:
        returncode = None

        async def wait(self) -> None:
            self.returncode = 0

    class PartialBrowser:
        def __init__(self, config: Any) -> None:
            del config
            self._process = PartialProcess()
            self.aclose_called = False

        async def start(self) -> None:
            start_entered.set()
            await asyncio.sleep(60)

        async def send(self, command: Any) -> None:
            del command
            cleanup_entered.set()
            await release_cleanup.wait()
            raise RuntimeError("not attached yet")

        async def aclose(self) -> None:
            self.aclose_called = True

    partial: PartialBrowser | None = None

    def browser_factory(config: Any) -> PartialBrowser:
        nonlocal partial
        partial = PartialBrowser(config)
        return partial

    monkeypatch.setattr(browser_module.nodriver, "Browser", browser_factory)
    manager = BrowserManager(
        settings_factory(
            browser_executable_path=tmp_path / "helium.exe",
            browser_start_timeout_seconds=30,
        )
    )
    startup = asyncio.create_task(manager.ensure_started())
    await start_entered.wait()

    startup.cancel()
    await cleanup_entered.wait()
    startup.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await startup

    assert partial is not None
    assert partial._process.returncode == 0
    assert partial.aclose_called is True


@pytest.mark.asyncio
async def test_attach_mode_hides_human_tabs_and_opens_owned_tab(settings_factory) -> None:
    human_page = FakePage("human", "https://mail.example/private?token=secret")
    browser = FakeBrowser([human_page])
    starter_calls: list[dict[str, Any]] = []

    async def starter(**kwargs: Any) -> FakeBrowser:
        starter_calls.append(kwargs)
        return browser

    manager = BrowserManager(
        settings_factory(connect_host="127.0.0.1", connect_port=9222),
        starter=starter,
    )
    attached = await manager.ensure_started()

    initial_status = await manager.status()
    assert initial_status["tabs"] == []
    assert initial_status["unmanaged_tab_count"] == 1

    mcp_page = await manager.open(attached, "https://example.com", new_tab=False)
    status = await manager.status()

    assert starter_calls == [{"host": "127.0.0.1", "port": 9222}]
    assert browser.get_calls == [("https://example.com", True)]
    assert human_page.navigations == []
    assert status["unmanaged_tab_count"] == 1
    assert [tab["id"] for tab in status["tabs"]] == [mcp_page.target.target_id]

    mcp_page.socket = type("ClosedSocket", (), {"close_code": 1006})()
    assert await manager.close() is True
    assert human_page.closed is False
    assert mcp_page.closed is True
    assert mcp_page.aclose_called is True
    assert browser.aclose_called is True


@pytest.mark.asyncio
async def test_direct_websocket_attach_uses_helium_approval_endpoint(
    settings_factory, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    websocket_url = "ws://127.0.0.1:9333/devtools/browser/nodriver-mcp"

    class DirectBrowser(FakeBrowser):
        def __init__(self, config: Any) -> None:
            super().__init__([FakePage("human")])
            self.config = config
            self.websocket_url = ""
            self.attach_called = False
            self.update_called = False

        async def attach(self) -> None:
            self.attach_called = True

        async def update_targets(self) -> None:
            self.update_called = True

    direct: DirectBrowser | None = None

    def browser_factory(config: Any) -> DirectBrowser:
        nonlocal direct
        direct = DirectBrowser(config)
        return direct

    monkeypatch.setattr(browser_module.nodriver, "Browser", browser_factory)
    manager = BrowserManager(
        settings_factory(
            browser_executable_path=tmp_path / "helium.exe",
            connect_websocket_url=websocket_url,
        )
    )

    running = await manager.ensure_started()

    assert running is direct
    assert direct is not None
    assert direct.websocket_url == websocket_url
    assert direct.attach_called is True
    assert direct.update_called is True
    assert (await manager.status())["tabs"] == []


@pytest.mark.asyncio
async def test_existing_tab_navigation_reuses_its_cdp_session(settings_factory) -> None:
    initial = FakePage("initial")
    browser = FakeBrowser([initial])

    async def starter(**kwargs: Any) -> FakeBrowser:
        del kwargs
        return browser

    manager = BrowserManager(settings_factory(), starter=starter)
    running = await manager.ensure_started()
    page = await manager.open(running, "https://example.com/same", new_tab=False)

    assert page is initial
    assert initial.navigations == ["https://example.com/same"]
    assert browser.get_calls == []


@pytest.mark.asyncio
async def test_manually_closed_active_tab_never_silently_retargets(settings_factory) -> None:
    active = FakePage("active", "https://example.com/form")
    other = FakePage("other", "https://example.com/other")

    class RefreshingBrowser(FakeBrowser):
        remove_active = False

        async def update_targets(self) -> None:
            if self.remove_active:
                self.tabs = [page for page in self.tabs if page is not active]

    browser = RefreshingBrowser([active, other])

    async def starter(**kwargs: Any) -> RefreshingBrowser:
        del kwargs
        return browser

    manager = BrowserManager(settings_factory(), starter=starter)
    await manager.ensure_started()
    browser.remove_active = True

    async with manager.operation() as running:
        with pytest.raises(BrowserError, match="active tab was closed"):
            manager.resolve_tab(running)

    async with manager.operation() as running:
        replacement = await manager.open(running, "https://example.com/new")

    assert other.navigations == []
    assert replacement is not other
    assert browser.get_calls == [("https://example.com/new", True)]
    assert active.aclose_called is True


@pytest.mark.asyncio
async def test_operation_deadline_releases_global_lock(settings_factory) -> None:
    browser = FakeBrowser([FakePage("initial")])

    async def starter(**kwargs: Any) -> FakeBrowser:
        del kwargs
        return browser

    manager = BrowserManager(
        settings_factory(operation_timeout_seconds=0.01),
        starter=starter,
    )

    with pytest.raises(BrowserError, match="operation exceeded"):
        async with manager.operation():
            await asyncio.sleep(0.05)

    assert manager.busy is False
    async with manager.operation() as restarted:
        assert restarted is browser


@pytest.mark.asyncio
async def test_operation_does_not_relabel_nested_timeout(settings_factory) -> None:
    browser = FakeBrowser([FakePage("initial")])

    async def starter(**kwargs: Any) -> FakeBrowser:
        del kwargs
        return browser

    manager = BrowserManager(settings_factory(), starter=starter)

    with pytest.raises(TimeoutError, match="inner operation"):
        async with manager.operation():
            raise TimeoutError("inner operation")


@pytest.mark.asyncio
async def test_failed_managed_close_is_reported_and_can_be_retried(settings_factory) -> None:
    class FailingProcess:
        returncode = None

        def __init__(self) -> None:
            self.terminate_called = False
            self.kill_called = False

        async def wait(self) -> None:
            raise TimeoutError

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

    class FailingBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__([FakePage("initial")])
            self._process = FailingProcess()

        async def send(self, command: Any) -> None:
            del command
            raise RuntimeError("disconnected")

    browser = FailingBrowser()

    async def starter(**kwargs: Any) -> FailingBrowser:
        del kwargs
        return browser

    manager = BrowserManager(settings_factory(), starter=starter)
    await manager.ensure_started()

    with pytest.raises(BrowserError, match="Browser shutdown failed"):
        await manager.close()

    assert browser._process.terminate_called is True
    assert browser._process.kill_called is True
    assert manager.browser is browser
    assert manager.busy is False


@pytest.mark.asyncio
async def test_navigation_accepts_redirect_back_to_starting_url_and_removes_handlers() -> None:
    class RedirectingPage(FakePage):
        def __init__(self) -> None:
            super().__init__("redirecting", "https://example.com/old")
            self.handlers: dict[type[Any], list[Any]] = {}

        def add_handler(self, event_type: type[Any], callback: Any) -> None:
            self.handlers.setdefault(event_type, []).append(callback)

        def remove_handler(self, event_type: type[Any], callback: Any) -> bool:
            callbacks = self.handlers.get(event_type, [])
            callbacks.remove(callback)
            return True

        def emit(self, event_type: type[Any], event: Any) -> None:
            for callback in list(self.handlers.get(event_type, [])):
                callback(event, self)

        async def send(self, command: Any) -> Any:
            payload = next(command)
            method = payload["method"]
            if method in {"Page.enable", "Page.setLifecycleEventsEnabled"}:
                return None
            if method == "Page.navigate":
                self.target.url = "https://example.com/old"
                self.emit(
                    browser_module.nodriver.cdp.page.FrameNavigated,
                    SimpleNamespace(
                        frame=SimpleNamespace(id_="main", loader_id="loader-a", parent_id=None)
                    ),
                )
                self.emit(
                    browser_module.nodriver.cdp.page.FrameStartedNavigating,
                    SimpleNamespace(frame_id="main", loader_id="loader-b"),
                )
                self.emit(
                    browser_module.nodriver.cdp.page.FrameNavigated,
                    SimpleNamespace(
                        frame=SimpleNamespace(id_="main", loader_id="loader-b", parent_id=None)
                    ),
                )
                return "main", "loader-a", None, None
            if method == "Page.getFrameTree":
                return SimpleNamespace(frame=SimpleNamespace(id_="main", loader_id="loader-b"))
            raise AssertionError(method)

        async def evaluate(
            self,
            expression: str,
            *,
            await_promise: bool,
            return_by_value: bool,
        ) -> str:
            del await_promise, return_by_value
            if "return true" in expression:
                raise RuntimeError("old execution context was unavailable")
            return json.dumps(
                {
                    "url": self.target.url,
                    "ready": "complete",
                    "oldDocument": False,
                }
            )

    page = RedirectingPage()

    result = await navigate_page(page, "https://example.com/start", timeout_seconds=1)

    assert result is page
    assert page.target.url == "https://example.com/old"
    assert all(not callbacks for callbacks in page.handlers.values())


@pytest.mark.asyncio
async def test_cancelled_create_closes_only_exact_target_after_late_response() -> None:
    existing_human = FakePage("human-existing", "https://example.com/human")
    late_human = FakePage("human-late", "https://example.com/other")
    owned = FakePage("mcp-owned", "https://example.com/result")

    class DelayedCreateBrowser(FakeBrowser):
        def __init__(self) -> None:
            super().__init__([existing_human])
            self.create_started = asyncio.Event()
            self.release_response = asyncio.Event()
            self.target_closed = asyncio.Event()
            self.closed_ids: list[str] = []

        async def send(self, command: Any) -> Any:
            payload = next(command)
            if payload["method"] == "Target.createTarget":
                self.tabs.append(owned)
                self.create_started.set()
                await self.release_response.wait()
                return owned.target.target_id
            if payload["method"] == "Target.closeTarget":
                identifier = str(payload["params"]["targetId"])
                self.closed_ids.append(identifier)
                self.tabs = [page for page in self.tabs if page.target.target_id != identifier]
                self.target_closed.set()
                return True
            raise AssertionError(payload["method"])

    browser = DelayedCreateBrowser()
    owned_ids: list[str] = []
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    tracked_tasks: set[asyncio.Future[Any]] = set()

    def tracking_factory(
        task_loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any
    ) -> asyncio.Future[Any]:
        task = asyncio.Task(coroutine, loop=task_loop, **kwargs)
        tracked_tasks.add(task)
        return task

    loop.set_task_factory(tracking_factory)
    try:
        creation = asyncio.create_task(
            create_new_page(
                browser,
                owned.target.url,
                timeout_seconds=10,
                on_target_created=owned_ids.append,
            )
        )
        await browser.create_started.wait()
        browser.tabs.append(late_human)

        def repeat_cancel_and_sweep_tracked_children() -> None:
            creation.cancel()
            for task in tracked_tasks:
                if task is not creation and not task.done():
                    task.cancel()

        creation.cancel()
        loop.call_later(0.01, repeat_cancel_and_sweep_tracked_children)
        loop.call_later(0.02, browser.release_response.set)
        with pytest.raises(asyncio.CancelledError):
            await creation

        await asyncio.wait_for(browser.target_closed.wait(), timeout=1)
        await asyncio.sleep(0)

        assert owned_ids == ["mcp-owned"]
        assert browser.closed_ids == ["mcp-owned"]
        assert owned.aclose_called is True
        assert [page.target.target_id for page in browser.tabs] == [
            "human-existing",
            "human-late",
        ]
        assert existing_human.closed is False
        assert late_human.closed is False
    finally:
        loop.set_task_factory(previous_factory)
