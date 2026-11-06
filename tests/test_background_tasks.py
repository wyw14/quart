from __future__ import annotations

import asyncio
import time
from dataclasses import fields

from quart import BackgroundTaskInfo
from quart import BackgroundTasksSnapshot
from quart import BackgroundTaskStatus
from quart import current_app
from quart import Quart


async def test_background_task() -> None:
    app = Quart(__name__)
    app.config["DATA"] = "data"

    data = None

    async def background() -> None:
        nonlocal data
        await asyncio.sleep(0.5)
        data = current_app.config["DATA"]

    @app.route("/")
    async def index() -> str:
        app.add_background_task(background)
        return ""

    async with app.test_app():
        test_client = app.test_client()
        await test_client.get("/")

    assert data == "data"


async def test_lifespan_background_task() -> None:
    app = Quart(__name__)
    app.config["DATA"] = "data"

    data = None

    async def background() -> None:
        nonlocal data
        await asyncio.sleep(0.5)
        data = current_app.config["DATA"]

    @app.before_serving
    async def startup() -> None:
        app.add_background_task(background)

    async with app.test_app():
        pass

    assert data == "data"


async def test_sync_background_task() -> None:
    app = Quart(__name__)
    app.config["DATA"] = "data"

    data = None

    def background() -> None:
        nonlocal data
        time.sleep(0.5)
        data = current_app.config["DATA"]

    @app.route("/")
    async def index() -> str:
        app.add_background_task(background)
        return ""

    async with app.test_app():
        test_client = app.test_client()
        await test_client.get("/")

    assert data == "data"


def test_background_tasks_snapshot_empty() -> None:
    app = Quart(__name__)

    snapshot = app.background_tasks_snapshot()

    assert isinstance(snapshot, BackgroundTasksSnapshot)
    assert snapshot.tasks == ()
    assert snapshot.total == 0
    assert snapshot.pending == 0
    assert snapshot.completed == 0
    assert snapshot.failed == 0
    assert snapshot.cancelled == 0


async def test_background_tasks_snapshot_creation_order() -> None:
    app = Quart(__name__)
    gates = [asyncio.Event() for _ in range(3)]

    async def background(index: int) -> None:
        await gates[index].wait()

    @app.before_serving
    async def startup() -> None:
        for index in range(3):
            app.add_background_task(background, index)

    async with app.test_app():
        snapshot = app.background_tasks_snapshot()
        assert snapshot.total == 3
        assert snapshot.pending == 3
        assert [task.id for task in snapshot.tasks] == [1, 2, 3]
        assert [task.status for task in snapshot.tasks] == [
            BackgroundTaskStatus.PENDING,
            BackgroundTaskStatus.PENDING,
            BackgroundTaskStatus.PENDING,
        ]

        # Tasks finish in reverse creation order, concurrently.
        gates[2].set()
        await asyncio.sleep(0.05)
        partial = app.background_tasks_snapshot()
        assert partial.pending == 2
        assert partial.completed == 1
        assert partial.tasks[2].status is BackgroundTaskStatus.COMPLETED

        # The earlier snapshot is a copy and remains unchanged.
        assert all(
            task.status is BackgroundTaskStatus.PENDING for task in snapshot.tasks
        )

        gates[1].set()
        gates[0].set()

    snapshot = app.background_tasks_snapshot()
    assert snapshot.pending == 0
    assert snapshot.completed == 3
    assert snapshot.failed == 0
    assert snapshot.cancelled == 0
    assert [task.id for task in snapshot.tasks] == [1, 2, 3]
    assert {task.status for task in snapshot.tasks} == {BackgroundTaskStatus.COMPLETED}
    # Finished tasks are released, no mutable task references retained.
    assert app.background_tasks == set()


async def test_background_tasks_snapshot_concurrent_endings() -> None:
    app = Quart(__name__)
    release = asyncio.Event()

    async def succeed() -> None:
        await release.wait()

    async def fail() -> None:
        await release.wait()
        raise RuntimeError("boom")

    @app.before_serving
    async def startup() -> None:
        app.add_background_task(succeed)
        app.add_background_task(fail)
        app.add_background_task(succeed)
        app.add_background_task(fail)

    async with app.test_app():
        release.set()
        await asyncio.sleep(0.05)

        snapshot = app.background_tasks_snapshot()

    assert snapshot.total == 4
    assert snapshot.pending == 0
    assert snapshot.completed == 2
    assert snapshot.failed == 2
    assert snapshot.cancelled == 0
    assert [task.id for task in snapshot.tasks] == [1, 2, 3, 4]
    assert [task.status for task in snapshot.tasks] == [
        BackgroundTaskStatus.COMPLETED,
        BackgroundTaskStatus.FAILED,
        BackgroundTaskStatus.COMPLETED,
        BackgroundTaskStatus.FAILED,
    ]


async def test_background_tasks_snapshot_does_not_leak_details() -> None:
    app = Quart(__name__)

    async def background(secret: str) -> None:
        await asyncio.sleep(0)
        raise ValueError(f"sensitive-{secret}")

    @app.route("/")
    async def index() -> str:
        app.add_background_task(background, "secret-argument")
        return ""

    async with app.test_app():
        test_client = app.test_client()
        await test_client.get("/")

    snapshot = app.background_tasks_snapshot()
    assert snapshot.failed == 1
    assert snapshot.completed == 0
    assert snapshot.cancelled == 0
    assert snapshot.pending == 0
    assert snapshot.tasks[0].status is BackgroundTaskStatus.FAILED

    # Only id and status are exposed per task - no arguments,
    # exception text, or mutable task references.
    assert [field.name for field in fields(BackgroundTaskInfo)] == ["id", "status"]
    rendered = repr(snapshot)
    assert "secret-argument" not in rendered
    assert "sensitive" not in rendered
    for task in snapshot.tasks:
        assert not isinstance(task, asyncio.Task)


async def test_background_tasks_snapshot_shutdown_timeout() -> None:
    app = Quart(__name__)
    app.config["BACKGROUND_TASK_SHUTDOWN_TIMEOUT"] = 0.05
    after_serving_called = False

    async def background() -> None:
        await asyncio.sleep(60)

    @app.before_serving
    async def startup() -> None:
        app.add_background_task(background)

    @app.after_serving
    async def after_serving() -> None:
        nonlocal after_serving_called
        after_serving_called = True

    async with app.test_app():
        snapshot = app.background_tasks_snapshot()
        assert snapshot.pending == 1
        assert snapshot.cancelled == 0
        assert snapshot.tasks[0].status is BackgroundTaskStatus.PENDING

    # The timeout cancels the task but shutdown still completes.
    assert after_serving_called
    snapshot = app.background_tasks_snapshot()
    assert snapshot.pending == 0
    assert snapshot.completed == 0
    assert snapshot.failed == 0
    assert snapshot.cancelled == 1
    assert snapshot.tasks[0].status is BackgroundTaskStatus.CANCELLED
    assert app.background_tasks == set()
