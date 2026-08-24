"""Every /internal route must actually require the service key.

main.py has carried this comment since the routers were first registered:

    # Register routers - all /internal/* routes require X-Service-Key auth
    # (enforced per-router via the verify_service_key dependency).

It was prose, and prose does not fail a build. Two routers -- exports.py and
outlier_assist.py -- had no auth dependency of any kind, so any workload
inside the Container Apps environment could POST a project_id to
/internal/exports/shapefile with no header and receive a ZIP of that
project's entire collar table. The endpoint filters on project_id alone and
binds no workspace RLS context.

The mechanism that let it happen is worth naming: the shared dependency in
app/services/auth.py was copy-pasted into four routers as a local
`_check_service_key`, so "does this router have auth?" had five different
answers to look for, and two routers having none looked like a sixth
variation rather than an omission.

This test turns the comment into a gate. It walks the registered routes and
asserts each /internal one carries a header-checking dependency, whichever
spelling it uses.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app

#: Unauthenticated by design. Probes and scrapers carry no session, and
#: none of these expose tenant data. Anything added here needs a reason
#: written next to it.
_PUBLIC_INTERNAL_PATHS: frozenset[str] = frozenset()


def _all_api_routes(routes, prefix: str = "") -> list[tuple[str, APIRoute]]:
    """Every APIRoute reachable from `routes`, with its full mounted path.

    Walks lazily-included sub-routers instead of assuming `app.routes` is a
    flat list of APIRoute.

    That assumption held on FastAPI 0.135 and broke on 0.141:
    `include_router` no longer copies the sub-router's routes onto the
    parent, it appends one `fastapi.routing._IncludedRouter` wrapper per
    call. On 0.141 this whole module found **zero** /internal routes, so the
    gate that proves every internal endpoint checks X-Service-Key — written
    after two routers were found serving a project's entire collar table to
    any unauthenticated caller inside the Container Apps environment — was
    asserting over an empty list.

    It failed loudly rather than passing vacuously, which is the only reason
    this was noticed at all; the `assert routes` line below was put there for
    exactly this. But a red that is really "your FastAPI is too new" reads
    like a flake, and the obvious way to quieten it is to relax the assert,
    at which point the gate is green and checking nothing.

    Worth knowing WHY the versions differ: pyproject pins `fastapi>=0.136.0`
    with no upper bound, so the resolved version depends on when the
    environment was built. See the open finding about the production image
    ignoring uv.lock — this is the same divergence, showing up in a test.
    """
    found: list[tuple[str, APIRoute]] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append((prefix + route.path, route))
            continue
        # FastAPI >= 0.141: a lazy include wrapper. Its context carries the
        # prefix that include_router() was called with.
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            sub_prefix = getattr(context, "prefix", "") or ""
            found.extend(_all_api_routes(inner.routes, prefix + sub_prefix))
            continue
        # Starlette Mount / Host and anything else with nested routes.
        nested = getattr(route, "routes", None)
        if nested:
            found.extend(_all_api_routes(nested, prefix + getattr(route, "path", "")))
    return found


def _internal_routes() -> list[tuple[str, APIRoute]]:
    return [
        (path, route)
        for path, route in _all_api_routes(app.routes)
        if path.startswith("/internal")
    ]


def _has_auth_dependency(route: APIRoute) -> bool:
    """True when anything in the route's dependency tree reads a key header.

    Deliberately shape-based rather than identity-based: it accepts the
    canonical `verify_service_key`, the four local `_check_service_key`
    clones and the `_check_diagnostic_auth` variant alike. The point is
    that SOMETHING checks a header, not that everyone spells it the same.
    """
    for dependency in route.dependant.dependencies:
        name = getattr(dependency.call, "__name__", "")
        if "service_key" in name or "diagnostic_auth" in name:
            return True
        if any(
            param.alias and param.alias.lower() == "x-service-key"
            for param in dependency.header_params
        ):
            return True

    if any(
        param.alias and param.alias.lower() == "x-service-key"
        for param in route.dependant.header_params
    ):
        return True

    return _body_checks_auth(route)


#: Auth helpers invoked from inside a handler body rather than injected as a
#: dependency. Each entry must be a function that raises HTTPException when the
#: caller is not authorised — grep before adding one.
_IN_BODY_AUTH_CALLS = (
    "_check_trigger_auth",      # integrations_trigger — per-flow Bearer JWT
    "verify_flow_jwt_token",
    "verify_service_key",
)


def _body_checks_auth(route: APIRoute) -> bool:
    """True when the handler calls a known auth helper itself.

    A dependency walk cannot see this. `POST /internal/v1/integrations/
    {flow_name}/trigger` takes `authorization: str | None = Header(...)` and
    calls `_check_trigger_auth(flow_name, authorization)` on its first line,
    because the check is per-flow: the JWT must carry `scope=flow:<flow_name>`
    for the flow named in the PATH, so it needs an argument a plain
    `Depends()` has no way to hand it. That is a deliberate design (each
    Kestra flow holds its own JWT, so a leak compromises one flow rather than
    every integration) and it is stricter than the shared X-Service-Key, not
    weaker.

    Without this branch the route reads as unprotected and the gate reports a
    vulnerability that is not there — which is as bad as missing a real one,
    because the next person to see this failure learns to distrust it.

    Source inspection rather than a hand-maintained allowlist of paths: an
    allowlist would still say "authorised" after someone deleted the call.
    """
    import inspect

    try:
        source = inspect.getsource(route.endpoint)
    except (OSError, TypeError):  # pragma: no cover — C-level or dynamic
        return False

    return any(f"{name}(" in source for name in _IN_BODY_AUTH_CALLS)


def test_every_internal_route_requires_the_service_key() -> None:
    routes = _internal_routes()
    assert routes, "no /internal routes registered - the app failed to import?"

    unprotected = sorted(
        f"{sorted(route.methods)} {path}"
        for path, route in routes
        if path not in _PUBLIC_INTERNAL_PATHS and not _has_auth_dependency(route)
    )

    assert not unprotected, (
        "These /internal routes accept requests with no X-Service-Key:\n  "
        + "\n  ".join(unprotected)
        + "\n\nAdd dependencies=[Depends(verify_service_key)] to the router."
    )


def test_the_two_routers_that_were_open_are_covered() -> None:
    """Named explicitly so a future refactor cannot quietly drop them."""
    paths = {path for path, route in _internal_routes() if _has_auth_dependency(route)}

    assert "/internal/exports/shapefile" in paths
    assert "/internal/exports/geopackage" in paths
    assert any(path.startswith("/internal/outlier-assist") for path in paths)


def test_the_per_flow_jwt_route_is_recognised_and_really_checks() -> None:
    """The integrations trigger is the only /internal route that authorises
    inside its handler body. Two assertions, because either alone is weak:
    that the gate SEES it, and that what it sees is a real call."""
    import inspect

    matches = [
        (path, route)
        for path, route in _internal_routes()
        if path == "/internal/v1/integrations/{flow_name}/trigger"
    ]

    assert len(matches) == 1, "route moved or disappeared — update this test"
    _path, route = matches[0]

    assert _has_auth_dependency(route)
    source = inspect.getsource(route.endpoint)
    assert "_check_trigger_auth(flow_name, authorization)" in source
