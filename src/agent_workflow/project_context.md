# Project context: Flask

Hand-written reference for reviewing pull requests against Flask (pallets/flask).
Source: the public Flask repository (`main` at commit 49943b5, Flask 3.2.0.dev),
its `docs/` folder and its PR template. This file is swappable: to review another
project, replace it. Each `##` section is one topic.

## Core paths

Changes here affect every Flask application, or code outside the diff depends on them.
One glob per bullet; `*` stays within one folder.

- `src/flask/app.py`
- `src/flask/sansio/*.py`
- `src/flask/ctx.py`
- `src/flask/globals.py`
- `src/flask/wrappers.py`
- `src/flask/sessions.py`
- `src/flask/helpers.py`
- `src/flask/config.py`
- `src/flask/__init__.py`
- `src/flask/blueprints.py`
- `src/flask/templating.py`
- `src/flask/json/*.py`

Why each one is core:
- app.py: the WSGI `Flask` class, request dispatch, error handling, default config.
- sansio/: app and blueprint registration shared with other implementations (e.g. Quart).
- ctx.py and globals.py: the app context and the `current_app`, `g`, `request`, `session` proxies.
- wrappers.py: the `Request` and `Response` classes, including request size limits.
- sessions.py: signed-cookie sessions and the session interface.
- helpers.py: `url_for`, `redirect`, `abort`, `flash`, `send_file`, `send_from_directory`.
- config.py: the `Config` object and every way of loading configuration.
- __init__.py: the public API; every name exported here is a compatibility promise.
- blueprints.py: the blueprint class used by most non-trivial apps.
- templating.py: Jinja integration, including autoescaping (XSS protection).
- json/: `jsonify`, request JSON parsing, and the tagged serializer used for session cookies.

Not core, but still reviewed as source: cli.py, testing.py, views.py, signals.py,
logging.py, debughelpers.py, typing.py.

## Architecture

- Flask is a WSGI framework built on Werkzeug (HTTP, routing, test client), Jinja
  (templates), itsdangerous (signing), blinker (signals) and click (CLI).
- `src/flask/sansio/` holds code with no I/O that alternative implementations reuse.
  It must not do I/O and must not use the Flask globals (`current_app`, `request`, ...).
- The `Flask` object in `app.py` subclasses `sansio.app.App` and adds the WSGI layer.
- Since 3.2, `RequestContext` is merged into `AppContext`, and dispatch methods such as
  `full_dispatch_request`, `preprocess_request` and `process_response` take the current
  `AppContext` as their first argument instead of reading the proxies.
- Request order inside `Flask.wsgi_app`: push the context, match the URL,
  `request_started` signal, `url_value_preprocessor` functions, `before_request`
  functions (a returned value short-circuits the view), the view, error handlers,
  response conversion, `after_this_request` and `after_request` functions, session save,
  `request_finished`, teardown functions, pop the context.
- Code placed before `before_request` in `full_dispatch_request` runs for every request,
  including static files and requests that match no route.

## Setup phase vs request handling

- All setup (routes, blueprints, error handlers, config, Jinja environment, session
  interface, JSON provider, extensions) must finish before the first request.
- Setup methods are decorated with `@setupmethod`, which raises an error once
  `_got_first_request` is set. Not every out-of-order change can be detected.
- Do not modify the `Flask` or `Blueprint` objects, or `app.config`, from inside view
  functions or other request handlers. Configuration is global, and a change made in one
  worker is not visible to the others.
- Code that reads `app.config` on every request should expect it to be constant.

## Concurrency and state

- A Flask app is served by many workers at once: threads, processes, greenlets or
  coroutines, sometimes on several machines. The same app object handles requests
  concurrently.
- Per-request data belongs in the context: `g`, `request`, `session`. These proxies
  are context-local (contextvars), so each worker sees its own values.
- Mutable state stored on the app object, on a module, or on a class is shared by every
  thread in a process and needs explicit locking. It is not shared between processes or
  machines, so counters or caches kept there differ per worker.
- The core request path keeps no mutable per-request state on the app object.
- `async def` views run in an event loop started in a thread for that request. They don't
  increase concurrency, and background tasks spawned inside them are cancelled when the
  view returns.

## Scope: what belongs in Flask core

- Flask is a "micro" framework: the core stays small and extensible.
- Databases, form validation, upload handling, authentication and other features with
  established libraries are provided by extensions, not by Flask itself.
- From `docs/design.rst`: "Flask itself just bridges to Werkzeug to implement a proper
  WSGI application and to Jinja to handle templating. [...] Everything else is up for
  extensions."

## Extensions and configuration

- Extensions follow the `init_app(app)` pattern and must not store the app on the
  extension (`self.app = app`); they use `current_app` at request time.
- Per-app extension state goes in `app.extensions` under a unique name.
- Extension config keys are prefixed with the extension's name.
- Built-in config keys are defined in `Flask.default_config` (app.py) and documented in
  `docs/config.rst`. A new built-in key needs a default, docs and a changelog entry.
- `app.config.from_prefixed_env()` loads `FLASK_*` environment variables.

## Security conventions

- `SECRET_KEY` signs session cookies; `SECRET_KEY_FALLBACKS` supports key rotation.
- The default session is a signed cookie, not an encrypted one: the client can read its
  contents but not modify them. Don't store secrets in `session`.
- Jinja autoescaping is on by default. Risky patterns: `Markup(...)` on user data,
  building HTML strings outside Jinja, unquoted attributes in templates.
- Serve user-chosen files with `send_from_directory` (uses `safe_join`), never by joining
  user input onto a path for `send_file`.
- `X-Forwarded-*` headers can be forged by any client. `request.access_route` and the
  other forwarded values are trustworthy only behind `ProxyFix`, configured with the
  real number of proxies. The docs call a wrong configuration a security issue
  (`docs/deploying/proxy_fix.rst`).
- The `Host` header is client-controlled. `TRUSTED_HOSTS` restricts it.
- Request size is limited by `MAX_CONTENT_LENGTH` (unset by default),
  `MAX_FORM_MEMORY_SIZE` (500kB) and `MAX_FORM_PARTS` (1000).
- Flask does not provide CSRF protection; that belongs to form extensions.

## Public API and compatibility

- The public API is what `flask/__init__.py` exports plus what `docs/api.rst` documents.
  Methods of `Flask`, `Blueprint`, `Request`, `Response` and `SessionInterface` are also
  subclassing API: users override them.
- Removing or renaming public names, or changing the signature of an overridable method,
  is a breaking change. Flask first deprecates: it emits a `DeprecationWarning` that names
  the version that will remove the old behavior, and keeps the old behavior working
  until then (e.g. 3.2 detects old dispatch-method signatures in `__init_subclass__`).
- Behavior changes are marked in docstrings with `.. versionadded::`,
  `.. versionchanged::` or `.. deprecated::`.
- Supported Python: 3.11 and newer.

## Contribution and testing conventions

- From the PR template: open an issue first (except for docs typos); add tests that fail
  without the change; update docs in `docs/` and in docstrings; add an entry to
  `CHANGES.rst`; add `.. versionchanged::` where relevant.
- Tests live in `tests/`, use pytest, and use the `app`, `client`, `app_ctx` and
  `req_ctx` fixtures from `tests/conftest.py`. pytest treats warnings as errors.
- Modules use `from __future__ import annotations` and `import typing as t`, and are
  fully type-annotated; mypy runs in strict mode on `src/`.
- ruff enforces style, including one import per line (isort `force-single-line`).
