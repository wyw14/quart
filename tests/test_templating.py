from __future__ import annotations

import asyncio

import pytest

from quart import Blueprint
from quart import diagnose_template_context_overrides
from quart import g
from quart import Quart
from quart import render_template_string
from quart import Response
from quart import ResponseReturnValue
from quart import session
from quart import stream_template_string
from quart import websocket


@pytest.fixture(scope="function")
def app() -> Quart:
    app = Quart(__name__)
    app.secret_key = "secret"

    return app


@pytest.fixture(scope="function")
def blueprint() -> Blueprint:
    blueprint = Blueprint("blueprint", __name__)

    @blueprint.route("/")
    def index() -> str:
        return ""

    return blueprint


async def test_template_render(app: Quart) -> None:
    async with app.app_context():
        rendered = await render_template_string("{{ foo }}", foo="bar")
    assert rendered == "bar"


async def test_default_template_context(app: Quart) -> None:
    async with app.app_context():
        g.foo = "bar"
        rendered = await render_template_string("{{ g.foo }}")
    assert rendered == "bar"
    async with app.test_request_context("/"):
        session["foo"] = "bar"
        rendered = await render_template_string(
            "{{ request.method }} {{ request.path }} {{ session.foo }}"
        )
    assert rendered == "GET / bar"


async def test_template_context_processors(app: Quart, blueprint: Blueprint) -> None:
    @blueprint.context_processor
    async def blueprint_context() -> dict:
        await asyncio.sleep(0.01)  # Test the ability to await
        return {"context": "foo"}

    @blueprint.app_context_processor
    async def app_blueprint_context() -> dict:
        return {"global_context": "boo"}

    @app.context_processor
    async def app_context() -> dict:
        return {"context": "bar"}

    app.register_blueprint(blueprint)

    async with app.app_context():
        rendered = await render_template_string("{{ context }}")
    assert rendered == "bar"

    async with app.test_request_context("/"):
        rendered = await render_template_string("{{ context }} {{ global_context }}")
    assert rendered == "foo boo"

    async with app.test_request_context("/other"):
        rendered = await render_template_string("{{ context }} {{ global_context }}")
    assert rendered == "bar boo"


async def test_template_context_overrides(app: Quart, blueprint: Blueprint) -> None:
    order: list[str] = []

    @blueprint.context_processor
    async def blueprint_shared() -> dict:
        order.append("blueprint")
        await asyncio.sleep(0.01)  # Test the ability to await
        return {"shared": "blueprint-secret", "blueprint_only": 1}

    @app.context_processor
    async def app_shared() -> dict:
        order.append("app-a")
        await asyncio.sleep(0.01)  # Test the ability to await
        return {"shared": "app-secret-a"}

    @app.context_processor
    def app_shared_sync() -> dict:
        order.append("app-b")
        return {"shared": "app-secret-b", "app_only": 2}

    app.register_blueprint(blueprint)

    async with app.test_request_context("/"):
        overrides = await diagnose_template_context_overrides()

    # Processors execute in registration order, async ones are awaited.
    assert order == ["app-a", "app-b", "blueprint"]

    assert [override.name for override in overrides] == ["shared"]
    sources = overrides[0].sources
    assert [source.scope for source in sources] == [None, None, "blueprint"]
    # App processors are indexes 0-2, the default processor is also
    # registered for the blueprint scope (index 3).
    assert [source.index for source in sources] == [1, 2, 4]
    assert sources[0].processor.endswith("app_shared")
    assert sources[1].processor.endswith("app_shared_sync")
    assert sources[2].processor.endswith("blueprint_shared")

    # Variable values are never included in the report.
    report = repr(overrides)
    assert "app-secret-a" not in report
    assert "app-secret-b" not in report
    assert "blueprint-secret" not in report

    # The diagnostic does not change the final context: the blueprint
    # processor wins amongst processors, explicit arguments win overall.
    async with app.test_request_context("/"):
        rendered = await render_template_string("{{ shared }}")
        assert rendered == "blueprint-secret"
        rendered = await render_template_string("{{ shared }}", shared="explicit")
        assert rendered == "explicit"


async def test_template_context_overrides_nested_blueprint(app: Quart) -> None:
    parent = Blueprint("parent", __name__, url_prefix="/parent")
    child = Blueprint("child", __name__, url_prefix="/child")

    @child.route("/")
    def index() -> str:
        return ""

    @parent.context_processor
    async def parent_shared() -> dict:
        return {"shared": "parent-secret", "parent_only": 1}

    @child.context_processor
    async def child_shared() -> dict:
        return {"shared": "child-secret", "child_only": 1}

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    async with app.test_request_context("/parent/child/"):
        overrides = await diagnose_template_context_overrides()

    assert [override.name for override in overrides] == ["shared"]
    sources = overrides[0].sources
    assert [source.scope for source in sources] == ["parent", "parent.child"]
    assert sources[0].processor.endswith("parent_shared")
    assert sources[1].processor.endswith("child_shared")
    assert sources[0].index < sources[1].index

    async with app.test_request_context("/parent/child/"):
        rendered = await render_template_string("{{ shared }}")
    assert rendered == "child-secret"

    # A request outside the blueprints runs only app processors.
    async with app.test_request_context("/other"):
        assert await diagnose_template_context_overrides() == []


async def test_template_context_overrides_websocket(
    app: Quart, blueprint: Blueprint
) -> None:
    @blueprint.websocket("/ws")
    async def ws() -> None:
        overrides = await diagnose_template_context_overrides()
        await websocket.send_json(
            [
                {
                    "name": override.name,
                    "scopes": [source.scope for source in override.sources],
                }
                for override in overrides
            ]
        )

    @blueprint.context_processor
    async def blueprint_shared() -> dict:
        return {"shared": "blueprint"}

    @app.context_processor
    async def app_shared() -> dict:
        return {"shared": "app"}

    app.register_blueprint(blueprint, url_prefix="/blueprint")

    test_client = app.test_client()
    async with test_client.websocket("/blueprint/ws") as test_ws:
        data = await test_ws.receive_json()

    assert data == [{"name": "shared", "scopes": [None, "blueprint"]}]


async def test_template_context_without_overrides(app: Quart) -> None:
    @app.context_processor
    async def unique_processor() -> dict:
        return {"unique": 1}

    async with app.app_context():
        assert await diagnose_template_context_overrides() == []


async def test_template_context_overrides_requires_app_context() -> None:
    with pytest.raises(RuntimeError):
        await diagnose_template_context_overrides()


async def test_template_context_globals(app: Quart, blueprint: Blueprint) -> None:
    @blueprint.app_template_global()
    def blueprint_global(value: str) -> str:
        return value.upper()

    @app.template_global()
    def app_global(value: str) -> str:
        return value.lower()

    app.register_blueprint(blueprint)

    async with app.app_context():
        rendered = await render_template_string(
            "{{ app_global('BAR') }} {{ blueprint_global('foo') }}"
        )
    assert rendered == "bar FOO"


async def test_template_filters(app: Quart, blueprint: Blueprint) -> None:
    @blueprint.app_template_filter()
    def blueprint_filter(value: str) -> str:
        return value.upper()

    @app.template_filter()
    def app_filter(value: str) -> str:
        return value.lower()

    app.register_blueprint(blueprint)

    async with app.app_context():
        rendered = await render_template_string("{{ 'App' | app_filter }}")
    assert rendered == "app"

    async with app.test_request_context("/"):
        rendered = await render_template_string("{{ 'App' | blueprint_filter }}")
    assert rendered == "APP"


async def test_template_tests(app: Quart, blueprint: Blueprint) -> None:
    @blueprint.app_template_test()
    def blueprint_test(value: int) -> bool:
        return value == 5

    @app.template_test()
    def app_test(value: int) -> bool:
        return value == 3

    app.register_blueprint(blueprint)

    async with app.app_context():
        rendered = await render_template_string("{% if 3 is app_test %}foo{% endif %}")
    assert rendered == "foo"

    async with app.test_request_context("/"):
        rendered = await render_template_string(
            "{% if 5 is blueprint_test %}bar{% endif %}"
        )
    assert rendered == "bar"


async def test_simple_stream(app: Quart) -> None:
    @app.get("/")
    async def index() -> ResponseReturnValue:
        return await stream_template_string("{{ config }}", config=42)  # type: ignore

    test_client = app.test_client()
    response = await test_client.get("/")
    assert (await response.data) == b"42"

    @app.get("/2")
    async def index2() -> ResponseReturnValue:
        return Response(await stream_template_string("{{ config }}", config=43))

    test_client = app.test_client()
    response = await test_client.get("/2")
    assert (await response.data) == b"43"
