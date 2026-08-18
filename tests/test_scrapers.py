from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nodriver_mcp.scrapers import ScrapeContext, ScraperError, ScriptRunner, ScriptStore

VALID_SOURCE = """\
async def scrape(ctx, params):
    return {"value": params.get("value")}
"""


@dataclass
class FakeTarget:
    target_id: str
    url: str = "about:blank"
    title: str = ""


class FakePage:
    def __init__(self, identifier: str) -> None:
        self.target = FakeTarget(identifier)
        self.closed = False
        self.aclose_called = False

    async def get(self, url: str) -> FakePage:
        self.target.url = url
        return self

    async def send(self, command) -> tuple[None, str, None, None]:
        payload = next(command)
        if payload["method"] == "Page.getFrameTree":
            return SimpleNamespace(frame=SimpleNamespace(loader_id="loader"))  # type: ignore[return-value]
        self.target.url = payload["params"]["url"]
        return None, "loader", None, None

    async def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = False,
        return_by_value: bool = False,
    ) -> str:
        del expression, await_promise, return_by_value
        return json.dumps({"url": self.target.url, "ready": "complete"})

    async def close(self) -> None:
        self.closed = True

    async def attach(self) -> None:
        return None

    async def aclose(self) -> None:
        self.aclose_called = True


class FakeBrowser:
    def __init__(self) -> None:
        self.created_pages: list[FakePage] = []

    async def get(self, url: str, *, new_tab: bool) -> FakePage:
        assert new_tab is True
        page = FakePage(f"extra-{len(self.created_pages) + 1}")
        page.target.url = url
        self.created_pages.append(page)
        return page

    async def send(self, command) -> object:
        payload = next(command)
        if payload["method"] == "Target.createTarget":
            page = FakePage(f"extra-{len(self.created_pages) + 1}")
            page.target.url = payload["params"]["url"]
            self.created_pages.append(page)
            return page.target.target_id
        if payload["method"] == "Target.closeTarget":
            identifier = str(payload["params"]["targetId"])
            for page in self.created_pages:
                if page.target.target_id == identifier:
                    page.closed = True
            return True
        raise AssertionError(payload["method"])

    async def update_targets(self) -> None:
        return None

    @property
    def tabs(self) -> list[FakePage]:
        return self.created_pages


def make_store(settings) -> ScriptStore:
    return ScriptStore(settings.scripts_dir, max_script_bytes=settings.max_script_bytes)


def test_script_store_save_read_list_and_compare_and_swap(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)

    first = store.save("products", VALID_SOURCE)
    assert first["name"] == "products"
    assert first["source"] == VALID_SOURCE
    assert first["bytes"] == len(VALID_SOURCE.encode())
    assert len(first["revision"]) == 16
    assert store.list() == [
        {
            "name": "products",
            "revision": first["revision"],
            "bytes": first["bytes"],
            "modified_at": first["modified_at"],
        }
    ]

    second_source = VALID_SOURCE.replace('params.get("value")', 'params.get("next")')
    second = store.save(
        "products",
        second_source,
        expected_revision=first["revision"],
    )
    assert second["revision"] != first["revision"]
    assert store.read("products")["source"] == second_source

    with pytest.raises(ScraperError, match="changed since it was read"):
        store.save("products", VALID_SOURCE, expected_revision=first["revision"])
    assert store.read("products")["revision"] == second["revision"]


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "nested/path",
        r"nested\path",
        "C:drive",
        "name.py",
        "_leading",
        "trailing.",
        "CON",
        "nul",
        "COM1",
        "LPT9",
        "a" * 65,
    ],
)
def test_script_store_rejects_unsafe_or_invalid_names(settings_factory, name: str) -> None:
    store = make_store(settings_factory())

    with pytest.raises(ScraperError, match="Scraper name"):
        store.path_for(name)


def test_script_store_rejects_symlink_escape(settings_factory, tmp_path: Path) -> None:
    settings = settings_factory()
    store = make_store(settings)
    store.ensure_root()
    outside = tmp_path / "outside.py"
    outside.write_text(VALID_SOURCE, encoding="utf-8")
    link = store.root / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")

    with pytest.raises(ScraperError, match="outside the configured scripts directory"):
        store.read("linked")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def scrape(ctx, params):\n    return {}\n", "must define `async def scrape"),
        ("async def scrape(ctx, params)\n    return {}\n", "syntax error"),
    ],
)
def test_script_store_validates_source(settings_factory, source: str, message: str) -> None:
    store = make_store(settings_factory())

    with pytest.raises(ScraperError, match=message):
        store.save("invalid", source)


def test_script_store_enforces_source_limit(settings_factory) -> None:
    settings = settings_factory(max_script_bytes=80)
    store = make_store(settings)

    with pytest.raises(ScraperError, match="limit is 80 bytes"):
        store.save("large", VALID_SOURCE + "#" * 80)


def test_script_store_requires_existing_file_for_expected_revision(settings_factory) -> None:
    store = make_store(settings_factory())

    with pytest.raises(ScraperError, match="scraper does not exist"):
        store.save("missing", VALID_SOURCE, expected_revision="0" * 16)


@pytest.mark.asyncio
async def test_runner_hot_reloads_same_size_source_with_unchanged_mtime(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    browser = FakeBrowser()
    page = FakePage("main")
    first_source = 'async def scrape(ctx, params):\n    return {"version": 1}\n'
    second_source = 'async def scrape(ctx, params):\n    return {"version": 2}\n'
    assert len(first_source) == len(second_source)

    first_record = store.save("reload", first_source)
    path = store.path_for("reload", must_exist=True)
    original_stat = path.stat()
    first_run = await runner.run("reload", {}, browser=browser, page=page)

    store.save("reload", second_source)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    second_run = await runner.run("reload", {}, browser=browser, page=page)

    assert first_run["result"] == {"version": 1}
    assert second_run["result"] == {"version": 2}
    assert first_run["revision"] == first_record["revision"]
    assert second_run["revision"] != first_run["revision"]


@pytest.mark.asyncio
async def test_runner_uses_fresh_module_globals_on_every_run(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    source = """\
counter = 0
async def scrape(ctx, params):
    global counter
    counter += 1
    return {"counter": counter}
"""
    store.save("fresh", source)
    browser = FakeBrowser()
    page = FakePage("main")

    first = await runner.run("fresh", {}, browser=browser, page=page)
    second = await runner.run("fresh", {}, browser=browser, page=page)

    assert first["result"] == {"counter": 1}
    assert second["result"] == {"counter": 1}


@pytest.mark.asyncio
async def test_runner_rejects_non_json_result(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save("non_json", "async def scrape(ctx, params):\n    return {1, 2}\n")

    with pytest.raises(ScraperError, match="result must be JSON-compatible"):
        await runner.run(
            "non_json",
            {},
            browser=FakeBrowser(),
            page=FakePage("main"),
        )


@pytest.mark.asyncio
async def test_runner_requires_exact_scrape_signature(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "signature",
        """\
async def scrape(ctx, params, optional=None):
    return {"optional": optional}
""",
    )

    with pytest.raises(ScraperError, match=r"must accept exactly \(ctx, params\)"):
        await runner.run(
            "signature",
            {},
            browser=FakeBrowser(),
            page=FakePage("main"),
        )


@pytest.mark.asyncio
async def test_runner_spills_large_json_result_to_artifact(settings_factory) -> None:
    settings = settings_factory(max_inline_result_bytes=32)
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "spill",
        'async def scrape(ctx, params):\n    return {"payload": "x" * 200}\n',
    )

    result = await runner.run(
        "spill",
        {},
        browser=FakeBrowser(),
        page=FakePage("main"),
    )

    assert result["result"] is None
    assert result["result_bytes"] > settings.max_inline_result_bytes
    assert result["artifact"] is not None
    artifact_path = Path(result["artifact"]["path"])
    assert artifact_path.parent == settings.artifacts_dir.resolve()
    assert artifact_path.suffix == ".json"
    artifact_stat = await asyncio.to_thread(artifact_path.stat)
    artifact_text = await asyncio.to_thread(artifact_path.read_text, encoding="utf-8")
    assert result["artifact"]["bytes"] == artifact_stat.st_size
    assert json.loads(artifact_text) == {"payload": "x" * 200}


@pytest.mark.asyncio
async def test_runner_closes_extra_tabs_after_success(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "tabs",
        """\
async def scrape(ctx, params):
    await ctx.new_page("https://example.test/one")
    await ctx.new_page("https://example.test/two")
    return {"ok": True}
""",
    )
    browser = FakeBrowser()

    result = await runner.run("tabs", {}, browser=browser, page=FakePage("main"))

    assert result["result"] == {"ok": True}
    assert len(browser.created_pages) == 2
    assert all(page.closed for page in browser.created_pages)
    assert all(page.aclose_called for page in browser.created_pages)


@pytest.mark.asyncio
async def test_runner_timeout_closes_extra_tabs(settings_factory) -> None:
    settings = settings_factory(scraper_timeout_seconds=1)
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "timeout",
        """\
import asyncio
async def scrape(ctx, params):
    await ctx.new_page()
    await asyncio.sleep(60)
""",
    )
    browser = FakeBrowser()

    with pytest.raises(ScraperError, match="1-second timeout"):
        await runner.run(
            "timeout",
            {},
            browser=browser,
            page=FakePage("main"),
            timeout_seconds=1,
        )

    assert len(browser.created_pages) == 1
    assert browser.created_pages[0].closed is True
    assert browser.created_pages[0].aclose_called is True


@pytest.mark.asyncio
async def test_runner_rejects_zero_timeout(settings_factory) -> None:
    settings = settings_factory(scraper_timeout_seconds=2)
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save("zero_timeout", VALID_SOURCE)

    modules_before = set(sys.modules)
    leaked_modules: set[str] = set()
    try:
        with pytest.raises(ScraperError, match="timeout_seconds must be between 1 and 2"):
            await runner.run(
                "zero_timeout",
                {},
                browser=FakeBrowser(),
                page=FakePage("main"),
                timeout_seconds=0,
            )
        leaked_modules = {
            name
            for name in set(sys.modules) - modules_before
            if name.startswith("_nodriver_scraper_zero_timeout_")
        }
        assert leaked_modules == set()
    finally:
        for name in leaked_modules:
            sys.modules.pop(name, None)


@pytest.mark.asyncio
async def test_concurrent_new_pages_reserve_the_tab_limit(settings_factory) -> None:
    settings = settings_factory(max_tabs_per_run=1)
    browser = FakeBrowser()
    context = ScrapeContext(browser, FakePage("main"), settings, "limit")

    results = await asyncio.gather(
        context.new_page("https://example.test/one"),
        context.new_page("https://example.test/two"),
        return_exceptions=True,
    )

    assert len(browser.created_pages) == 1
    assert sum(isinstance(result, ScraperError) for result in results) == 1
    await context.close_extra_tabs()
    assert browser.created_pages[0].aclose_called is True


@pytest.mark.asyncio
async def test_runner_tracks_generic_background_tasks(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "child_task",
        """\
import asyncio
async def scrape(ctx, params):
    task = asyncio.create_task(asyncio.sleep(60))
    params["tasks"].append(task)
    await asyncio.sleep(0)
    return {"ok": True}
""",
    )
    tasks: list[asyncio.Task[None]] = []

    result = await runner.run(
        "child_task",
        {"tasks": tasks},
        browser=FakeBrowser(),
        page=FakePage("main"),
    )

    assert result["result"] == {"ok": True}
    assert len(tasks) == 1
    assert tasks[0].cancelled() is True


@pytest.mark.asyncio
async def test_runner_preserves_listener_started_by_raw_nodriver_api(settings_factory) -> None:
    class RawPage(FakePage):
        def __init__(self, identifier: str) -> None:
            super().__init__(identifier)
            self.release_listener = asyncio.Event()
            self._listener_task: asyncio.Task[None] | None = None
            self.socket = None

        async def _listener(self) -> None:
            await self.release_listener.wait()

        async def raw_api(self) -> str:
            self._listener_task = asyncio.create_task(self._listener())
            keepalive_task = asyncio.create_task(self._listener())
            self.socket = type("FakeSocket", (), {"keepalive_task": keepalive_task})()
            await asyncio.sleep(0)
            return "raw-ok"

    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "raw_listener",
        """\
async def scrape(ctx, params):
    return {"value": await ctx.page.raw_api()}
""",
    )
    page = RawPage("main")

    result = await runner.run(
        "raw_listener",
        {},
        browser=FakeBrowser(),
        page=page,
    )

    assert result["result"] == {"value": "raw-ok"}
    assert page._listener_task is not None
    assert page._listener_task.done() is False
    assert page.socket is not None
    assert page.socket.keepalive_task.done() is False
    page.release_listener.set()
    await asyncio.wait_for(
        asyncio.gather(page._listener_task, page.socket.keepalive_task),
        timeout=1,
    )


@pytest.mark.asyncio
async def test_runner_cleanup_has_hard_deadline_for_stubborn_task(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "stubborn_task",
        """\
import asyncio
async def worker(stop, started):
    started.set()
    while not stop.is_set():
        try:
            await stop.wait()
        except asyncio.CancelledError:
            pass

async def scrape(ctx, params):
    task = asyncio.create_task(worker(params["stop"], params["started"]))
    params["tasks"].append(task)
    await params["started"].wait()
    return {"ok": True}
""",
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    before = time.perf_counter()
    result = await runner.run(
        "stubborn_task",
        {"stop": stop, "started": started, "tasks": tasks},
        browser=FakeBrowser(),
        page=FakePage("main"),
    )
    elapsed = time.perf_counter() - before

    assert result["result"] == {"ok": True}
    assert elapsed < 3
    assert tasks[0].done() is False
    stop.set()
    await asyncio.wait_for(tasks[0], timeout=1)


@pytest.mark.asyncio
async def test_runner_cleanup_rescans_for_late_grandchildren(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "grandchild",
        """\
import asyncio
async def worker(tasks):
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        tasks.append(asyncio.create_task(asyncio.sleep(60)))

async def scrape(ctx, params):
    task = asyncio.create_task(worker(params["tasks"]))
    params["tasks"].append(task)
    await asyncio.sleep(0)
    return {"ok": True}
""",
    )
    tasks: list[asyncio.Task[None]] = []

    await runner.run(
        "grandchild",
        {"tasks": tasks},
        browser=FakeBrowser(),
        page=FakePage("main"),
    )

    assert len(tasks) == 2
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_overlapping_runners_share_task_tracking_factory(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "overlap_a",
        """\
async def scrape(ctx, params):
    params["ready"].set()
    await params["release"].wait()
    return {"runner": "a"}
""",
    )
    store.save(
        "overlap_b",
        """\
import asyncio
async def scrape(ctx, params):
    params["ready"].set()
    await params["release"].wait()
    task = asyncio.create_task(asyncio.sleep(60))
    params["tasks"].append(task)
    await asyncio.sleep(0)
    return {"runner": "b"}
""",
    )
    a_ready = asyncio.Event()
    a_release = asyncio.Event()
    b_ready = asyncio.Event()
    b_release = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    run_a = asyncio.create_task(
        runner.run(
            "overlap_a",
            {"ready": a_ready, "release": a_release},
            browser=FakeBrowser(),
            page=FakePage("a"),
        )
    )
    await a_ready.wait()
    run_b = asyncio.create_task(
        runner.run(
            "overlap_b",
            {"ready": b_ready, "release": b_release, "tasks": tasks},
            browser=FakeBrowser(),
            page=FakePage("b"),
        )
    )
    await b_ready.wait()

    a_release.set()
    assert (await run_a)["result"] == {"runner": "a"}
    b_release.set()
    assert (await run_b)["result"] == {"runner": "b"}

    assert len(tasks) == 1
    assert tasks[0].done() is True


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_runner_cleanup(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "cancel_cleanup",
        """\
import asyncio
async def worker(started, tasks):
    started.set()
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        await asyncio.sleep(0.25)

async def scrape(ctx, params):
    task = asyncio.create_task(worker(params["started"], params["tasks"]))
    params["tasks"].append(task)
    await params["started"].wait()
    await asyncio.sleep(60)
""",
    )
    started = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    run = asyncio.create_task(
        runner.run(
            "cancel_cleanup",
            {"started": started, "tasks": tasks},
            browser=FakeBrowser(),
            page=FakePage("main"),
        )
    )
    await started.wait()

    run.cancel()
    await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert len(tasks) == 1
    assert tasks[0].done() is True


@pytest.mark.asyncio
async def test_inner_timeout_error_is_not_mislabeled_as_deadline(settings_factory) -> None:
    settings = settings_factory()
    store = make_store(settings)
    runner = ScriptRunner(store, settings)
    store.save(
        "inner_timeout",
        """\
async def scrape(ctx, params):
    raise TimeoutError("inner operation")
""",
    )

    with pytest.raises(ScraperError, match="Scraper failed") as caught:
        await runner.run(
            "inner_timeout",
            {},
            browser=FakeBrowser(),
            page=FakePage("main"),
        )

    assert "cooperative" not in str(caught.value)
