"""The noticeboard's HTTP surface (invariant 20).

Server-rendered HTML. No JavaScript build, no CDN, no framework on the
page: every page works with scripts off, because the page a reader
reaches when something is broken must not itself need something to work.

Three rules hold the perimeter, and each one has already been broken
somewhere in a service like this one.

1. **The access key is checked on every path that is not styling.** There
   is no "key not configured, skip the check" branch. `config.py` refuses
   to start with an empty key on a non-loopback bind, so the only way to
   reach an unkeyed page is to ask for one on loopback.
2. **Every state-changing POST carries a CSRF token AND a recognised
   sender.** The token is in a `SameSite=Strict; HttpOnly` cookie and in
   a hidden field. `SameSite=Strict` classifies by registrable domain, so
   a sibling subdomain behind the same proxy is still "same-site": the
   `Origin`/`Referer` check is what covers that, and it runs even when
   the token matches.
3. **The middleware never reads the body.** `BaseHTTPMiddleware` does not
   replay a consumed body stream to the route, so a form read in
   middleware silently empties the route's own form. The token's hidden
   field is read in the route, from the body the route itself reads.

The form body is parsed with `urllib.parse.parse_qsl`. An HTML form with
no file input posts `application/x-www-form-urlencoded`, so a multipart
parser would be a dependency and a parser this service does not need.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl

from agent_family import Diff, load_registry
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

from . import pages, security
from .auditfiles import ARGS_NOTICE, AuditFilter, known_days
from .config import Config
from .familyform import Form, document_of, form_of, parse_posted
from .pages import EditPage
from .registrywrite import family_path, save_family
from .security import CSRF_COOKIE, CSRF_FIELD, Origin, Refusal
from .sessions import SessionReader
from .yamlkeep import edited_text

HERE: Final = Path(__file__).parent
TEMPLATES: Final = HERE / "templates"
STATIC: Final = HERE / "static"

_FORBIDDEN: Final = 403
_SEE_OTHER: Final = 303
_MAX_BODY_BYTES: Final = 1 << 20

#: The two verbs the edit form posts. `preview` writes nothing.
VERB_PREVIEW: Final = "preview"
VERB_SAVE: Final = "save"


def build_app(
    config: Config, reader: SessionReader, clock: Callable[[], datetime] | None = None
) -> FastAPI:
    """The whole service.

    `reader` and `clock` are injected so a test needs no socket and no
    real time: staleness is a comparison against a clock, so a test that
    could not pin one could not test it.
    """
    app = FastAPI(title="noticeboard", docs_url=None, redoc_url=None, openapi_url=None)
    render = _renderer(config)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    _perimeter(app, config)
    _routes(app, config, reader, render, clock or _now)

    return app


def _renderer(config: Config) -> Callable[[Request, str, dict[str, object]], HTMLResponse]:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        # Every value on these pages came from another process or from a
        # family's own file. Escaping is on by default and never off.
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def render(request: Request, name: str, body: dict[str, object]) -> HTMLResponse:
        template = environment.get_template(name)
        shared: dict[str, object] = {
            "csrf_token": getattr(request.state, "csrf", ""),
            "csrf_field": CSRF_FIELD,
            "args_notice": ARGS_NOTICE,
            "bind": f"{config.bind}:{config.port}",
            # The two verb names live here, not in the template, so the
            # button and the branch that reads it cannot drift apart.
            "verb_preview": VERB_PREVIEW,
            "verb_save": VERB_SAVE,
        }

        return HTMLResponse(template.render(**shared, **body))

    return render


def _perimeter(app: FastAPI, config: Config) -> None:
    """Rules 1 and 2, minus the token's hidden field (rule 3)."""

    @app.middleware("http")
    async def _guard(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not security.is_keyless(request.url.path):
            refusal = security.check_key(
                request.headers.get(security.ACCESS_HEADER), config.access_key
            )

            if refusal is not None:
                return _refused(refusal)

        cookie = request.cookies.get(CSRF_COOKIE) or security.mint_csrf()
        request.state.csrf = cookie
        response = await call_next(request)
        _set_csrf(response, cookie, config)

        return response


def _set_csrf(response: Response, token: str, config: Config) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=config.cookie_secure,
        path="/",
    )


def _refused(refusal: Refusal) -> JSONResponse:
    """The word, never the value that failed to match."""
    return JSONResponse(
        {"ok": False, "error": refusal.value},
        status_code=_FORBIDDEN,
    )


def _routes(
    app: FastAPI,
    config: Config,
    reader: SessionReader,
    render: Callable[[Request, str, dict[str, object]], HTMLResponse],
    clock: Callable[[], datetime],
) -> None:
    @app.get("/healthz")
    def _healthz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Keyless, so a prober is not probing the proxy's login page."""
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def _home(request: Request) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return render(request, "home.html", {"page": pages.home(config, clock())})

    @app.get("/families/{name}", response_class=HTMLResponse)
    def _family(request: Request, name: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        body = pages.family_page(config, reader, name, clock())

        return render(request, "family.html", {"page": body})

    @app.get("/sessions/{family}/{session}", response_class=HTMLResponse)
    def _session(  # pyright: ignore[reportUnusedFunction]
        request: Request, family: str, session: str
    ) -> HTMLResponse:
        body = pages.session_page(reader, family, session)

        return render(request, "session.html", {"page": body})

    @app.get("/audit", response_class=HTMLResponse)
    def _audit(request: Request) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        wanted = _filter_of(request.query_params)
        offset = _offset_of(request.query_params)

        return render(
            request,
            "audit.html",
            {
                "page": pages.audit_page(config, wanted, offset),
                "wanted": wanted,
                "days": known_days(config.audit_dir),
            },
        )

    @app.get("/families/{name}/edit", response_class=HTMLResponse)
    def _edit(request: Request, name: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return render(request, "edit.html", {"page": pages.edit_page(config, name)})

    @app.post("/families/{name}/edit")
    async def _save(request: Request, name: str) -> Response:  # pyright: ignore[reportUnusedFunction]
        posted, refusal = await _form_of(request)

        if refusal is not None:
            return _refused(refusal)

        page = await run_in_threadpool(_apply, config, name, posted)

        if page.saved:
            # Redirect after a write, so a reload does not repost it.
            return RedirectResponse(f"/families/{name}?saved={page.saved}", status_code=_SEE_OTHER)

        return render(request, "edit.html", {"page": page})


def _apply(config: Config, name: str, posted: dict[str, str]) -> EditPage:
    """Preview or save, both from the same posted form."""
    registry = load_registry(config.registry_dir)
    current = registry.families.get(name)

    if current is None:
        return pages.edit_page(config, name)

    form = form_of(current, pages.index_of(registry))
    wanted, issues = parse_posted(form, posted)

    if wanted is None:
        return EditPage(family=name, form=form, issues=tuple(issues), posted=posted)

    diff = pages.preview_of(current, wanted)

    if posted.get("verb") != VERB_SAVE:
        return EditPage(family=name, form=form, diff=diff, posted=posted)

    models = (current.model_dump(mode="json"), wanted.model_dump(mode="json"))

    return _saved(config, name, form, diff, posted, models)


def _saved(
    config: Config,
    name: str,
    form: Form,
    diff: Diff,
    posted: dict[str, str],
    models: tuple[dict[str, Any], dict[str, Any]],
) -> EditPage:
    """The write. Everything before this point changed nothing on disk."""
    text, problem = document_of(form, posted)

    if problem:
        return EditPage(family=name, form=form, diff=diff, problem=problem, posted=posted)

    text = _kept(config, name, models) or text
    result = save_family(config.registry_dir, name, text, posted.get("subject", ""))

    if not result.ok:
        return EditPage(
            family=name,
            form=form,
            diff=diff,
            issues=result.issues,
            problem=result.problem,
            posted=posted,
        )

    # Re-read: the form now shows what the registry holds, not what was
    # typed. An unchanged save has no commit and says so.
    return EditPage(
        family=name,
        form=pages.edit_page(config, name).form,
        diff=diff,
        saved=result.commit or "unchanged",
    )


def _kept(config: Config, name: str, models: tuple[dict[str, Any], dict[str, Any]]) -> str | None:
    """The family file with only the changed fields replaced, so its
    comments survive the save (`yamlkeep.py`).

    `None` whenever the file cannot be read or round-tripped, and the caller
    then writes the emitter's document instead. A save must not fail over a
    comment.
    """
    before, after = models

    try:
        original = family_path(config.registry_dir, name).read_text(encoding="utf-8")
    except OSError:
        return None

    return edited_text(original, before, after)


async def _form_of(request: Request) -> tuple[dict[str, str], Refusal | None]:
    """The posted form, and the CSRF verdict (rules 2 and 3)."""
    raw = await request.body()

    if len(raw) > _MAX_BODY_BYTES:
        return {}, Refusal.BAD_TOKEN

    posted = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
    sender = Origin(
        origin=request.headers.get("origin"),
        referer=request.headers.get("referer"),
        host=request.headers.get("host"),
    )
    refusal = security.check_csrf(request.cookies.get(CSRF_COOKIE), posted.get(CSRF_FIELD), sender)

    return posted, refusal


def _filter_of(query: Mapping[str, str]) -> AuditFilter:
    return AuditFilter(
        family=_one(query, "family"),
        session=_one(query, "session"),
        tool=_one(query, "tool"),
        decision=_one(query, "decision"),
    )


#: A filter value is untrusted input and only ever compared with a field.
_MAX_QUERY_CHARS: Final = 200


def _one(query: Mapping[str, str], name: str) -> str:
    return query.get(name, "").strip()[:_MAX_QUERY_CHARS]


def _offset_of(query: Mapping[str, str]) -> int:
    try:
        return max(0, int(query.get("offset", "0")))
    except ValueError:
        return 0


def _now() -> datetime:
    return datetime.now(UTC)
