.. _templating:

Templates
=========

Quart uses the `Jinja <https://jinja.palletsprojects.com>`_ templating engine,
which is well `documented
<https://jinja.palletsprojects.com/templates/>`_. Quart adds a standard
context, and some standard filters to the Jinja defaults. Quart also
adds the ability to define custom filters, tests and contexts at an
app and blueprint level.

There are two functions to use when templating,
:func:`~quart.templating.render_template` and
:func:`~quart.templating.render_template_string`, both must be
awaited. The return value from either function is a string and can
form a route response directly or be otherwise combined. Both
functions take an variable number of additional keyword arguments to
pass to the template as context, for example,

.. code-block:: python

    @app.route('/')
    async def index():
        return await render_template('index.html', hello='world')

Quart standard extras
---------------------

The standard context includes the ``config``, ``request``,
``session``, and ``g`` with these objects referencing the
``current_app.config`` and those defined in :mod:`~quart.globals`
respectively. The can be accessed as expected,

.. code-block:: python

    @app.route('/')
    async def index():
        return await render_template_string("{{ request.endpoint }}")

The standard global functions are :func:`~quart.helpers.url_for` and
:func:`~quart.helpers.get_flashed_messages`. These can be used as expected,

.. code-block:: python

    @app.route('/')
    async def index():
        return await render_template_string("<a href="{{ url_for('index') }}>index</a>")

Adding filters, tests, globals and context
------------------------------------------

To add a filter for usage in templates, make use of
:meth:`~quart.app.Quart.template_filter` or
:meth:`~quart.blueprints.Blueprint.app_template_filter` as decorators,
or :meth:`~quart.app.Quart.add_template_filter` or
:meth:`~quart.blueprints.Blueprint.add_app_template_filter` as
functions. These expect the filter to take in Any value and return a
str, e.g.

.. code-block:: python

    @app.template_filter(name='upper')
    def upper_case(value):
        return value.upper()

    @app.route('/')
    async def index():
        return await render_template_string("{{ lower | upper }}")

tests and globals work in a very similar way only with the test and
global methods rather than filter.

The context processors however have an additional feature, in that
they can be specified on a per blueprint basis. This allows contextual
information to be present only for requests that are routed to the
blueprint. By default
:meth:`~quart.blueprints.Blueprint.context_processor` adds contextual
information to blueprint routed requests whereas
:meth:`~quart.blueprints.Blueprint.app_context_processor` adds the
information to all requests to the app. An example,

.. code-block:: python

    @blueprint.context_processor
    async def blueprint_only():
        return {'context': 'value'}

    @blueprint.app_context_processor
    async def app_wide():
        return {'context': 'value'}

Diagnosing context overrides
----------------------------

When more than one context processor sets the same variable the
processor that runs last wins, with variables passed directly to the
render call always winning. The processors run app first followed by
the blueprint processors from the parent blueprint down to the
blueprint the request is routed to.
:func:`~quart.templating.diagnose_template_context_overrides` awaits
the processors in this actual execution order for the current request
or websocket and reports each variable that is set more than once
together with the ordered sources that set it. Only variable names and
the processor origins are returned, never the values, and the template
context is not changed.

.. code-block:: python

    overrides = await diagnose_template_context_overrides()
    for override in overrides:
        print(override.name, [source.processor for source in override.sources])
