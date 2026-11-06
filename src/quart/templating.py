from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from typing import TYPE_CHECKING

from flask.templating import (
    DispatchingJinjaLoader as DispatchingJinjaLoader,  # noqa: F401
)
from jinja2 import Environment as BaseEnvironment
from jinja2 import Template

from .ctx import has_app_context
from .ctx import has_request_context
from .globals import app_ctx
from .globals import current_app
from .helpers import stream_with_context
from .signals import before_render_template
from .signals import template_rendered

if TYPE_CHECKING:
    from .app import Quart  # noqa
    from .ctx import _AppCtxGlobals
    from .sessions import SessionMixin
    from .wrappers import Request


@dataclass(frozen=True)
class TemplateContextProcessorSource:
    """The source of a variable injected by a context processor.

    Only the variable name and its origin are recorded, never the
    variable value.

    Attributes:
        index: Zero based position of the processor in the actual
            execution order, i.e. app processors first followed by the
            blueprint processors from the parent blueprint down to the
            blueprint the request is routed to.
        scope: Blueprint name the processor is registered for, or
            ``None`` for an app wide processor.
        processor: Qualified name of the processor callable
            (``module.qualname``).
    """

    index: int
    scope: str | None
    processor: str


@dataclass(frozen=True)
class TemplateContextOverride:
    """A template variable set by more than one context processor.

    Attributes:
        name: The template variable name that is set repeatedly.
        sources: The processors that set the variable, ordered by
            actual execution order. The last source in the tuple is the
            processor that wins (unless the variable is also passed
            explicitly to the render call, which always wins).
    """

    name: str
    sources: tuple[TemplateContextProcessorSource, ...]


def _describe_context_processor(processor: Any) -> str:
    """Return a value-free qualified name for *processor*."""
    qualname = getattr(processor, "__qualname__", None) or getattr(
        processor, "__name__", None
    )
    module = getattr(processor, "__module__", None)
    if qualname is not None and module is not None:
        return f"{module}.{qualname}"
    if qualname is not None:
        return qualname
    return repr(processor)


class Environment(BaseEnvironment):
    """Quart specific Jinja Environment.

    This changes the default Jinja loader to use the
    DispatchingJinjaLoader, and enables async Jinja by default.
    """

    def __init__(self, app: Quart, **options: Any) -> None:
        """Create a Quart specific Jinja Environment.

        Arguments:
            app: The Quart app to bind to.
            options: The standard Jinja Environment options.
        """
        if "loader" not in options:
            options["loader"] = app.create_global_jinja_loader()
        options["enable_async"] = True
        super().__init__(**options)


async def render_template(
    template_name_or_list: str | list[str], **context: Any
) -> str:
    """Render the template with the context given.

    Arguments:
        template_name_or_list: Template name to render of a list of
            possible template names.
        context: The variables to pass to the template.
    """
    ctx = app_ctx._get_current_object()
    await current_app.update_template_context(ctx, context)
    template = current_app.jinja_env.get_or_select_template(template_name_or_list)  # type: ignore
    return await _render(template, context, current_app._get_current_object())


async def render_template_string(source: str, **context: Any) -> str:
    """Render the template source with the context given.

    Arguments:
        source: The template source code.
        context: The variables to pass to the template.
    """
    ctx = app_ctx._get_current_object()
    await current_app.update_template_context(ctx, context)
    template = current_app.jinja_env.from_string(source)
    return await _render(template, context, current_app._get_current_object())


async def _render(template: Template, context: dict, app: Quart) -> str:
    await before_render_template.send_async(
        app,
        _sync_wrapper=app.ensure_async,  # type: ignore[arg-type]
        template=template,
        context=context,
    )
    rendered_template = await template.render_async(context)
    await template_rendered.send_async(
        app,
        _sync_wrapper=app.ensure_async,  # type: ignore[arg-type]
        template=template,
        context=context,
    )
    return rendered_template


async def _default_template_ctx_processor() -> dict[str, Any]:
    context: dict[str, _AppCtxGlobals | Request | SessionMixin] = {}
    ctx = app_ctx._get_current_object()
    if has_app_context():
        context["g"] = ctx.g
    if has_request_context():
        context["request"] = ctx.request
        context["session"] = ctx.session
    return context


async def diagnose_template_context_overrides() -> list[TemplateContextOverride]:
    """Diagnose template variables set by multiple context processors.

    The app and blueprint context processors are awaited in the same
    order as during a render (app processors first, then the blueprint
    processors from the parent blueprint down to the routed blueprint),
    and every variable name that is set by more than one processor is
    reported together with the ordered sources that set it.

    Only variable names and their processor sources are returned, never
    the variable values, and the diagnostic does not mutate or create a
    template context. This must be called within an app context, and
    blueprint processors are only considered within a request or
    websocket context.
    """
    ctx = app_ctx._get_current_object()
    return await current_app.diagnose_template_context_overrides(ctx)


async def stream_template(
    template_name_or_list: str | Template | list[str | Template], **context: Any
) -> AsyncIterator[str]:
    """Render a template by name with the given context as a stream.

    This returns an iterator of strings, which can be used as a
    streaming response from a view.

    Arguments:
        template_name_or_list: The name of the template to render. If a
            list is given, the first name to exist will be rendered.
        context: The variables to make available in the template.
    """
    ctx = app_ctx._get_current_object()
    await current_app.update_template_context(ctx, context)
    template = current_app.jinja_env.get_or_select_template(template_name_or_list)
    return await _stream(current_app._get_current_object(), template, context)


async def stream_template_string(source: str, **context: Any) -> AsyncIterator[str]:
    """Render a template from the given source with the *context* as a stream.

    This returns an iterator of strings, which can
    be used as a streaming response from a view.

    Arguments:
        source: The source code of the template to render.
        context: The variables to make available in the template.
    """
    ctx = app_ctx._get_current_object()
    await current_app.update_template_context(ctx, context)
    template = current_app.jinja_env.from_string(source)
    return await _stream(current_app._get_current_object(), template, context)


async def _stream(
    app: Quart, template: Template, context: dict[str, Any]
) -> AsyncIterator[str]:
    await before_render_template.send_async(
        app,
        _sync_wrapper=app.ensure_async,  # type: ignore[arg-type]
        template=template,
        context=context,
    )

    async def generate() -> AsyncIterator[str]:
        async for chunk in template.generate_async(context):
            yield chunk
        await template_rendered.send_async(
            app,
            _sync_wrapper=app.ensure_async,  # type: ignore[arg-type]
            template=template,
            context=context,
        )

    # If a request context is active, keep it while generating.
    if has_request_context():
        return stream_with_context(generate)()
    else:
        return generate()
