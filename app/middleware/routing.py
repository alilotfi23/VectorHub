"""Post-routing path templating shared by the request-observation middlewares.

After Starlette routes a request it sets ``scope["route"]``; its
``route.path`` is *router-relative* though (it misses the ``/api/v1`` mount
prefix). Reconstructing the full template from the matched path and its
captured path params gives the canonical label used by both the request
counter and the trace span: ``/api/v1/tenants/{tenant_id}`` — bounded series,
no per-literal-path explosion.
"""

from typing import Any, cast

from starlette.types import Scope


def route_template(scope: Scope) -> str:
    """The request's path with dynamic segments collapsed to ``{name}``.

    Falls back to the raw path for unmatched requests (404s), where no route
    was resolved.
    """
    route = cast(dict[str, Any], scope).get("route")
    path: str = scope["path"]
    if route is None:
        return path
    path_params = cast(dict[str, Any], scope).get("path_params", {})
    for name, value in path_params.items():
        path = path.replace(str(value), "{" + name + "}")
    return path
