"""Route lookup that works across FastAPI versions.

Older FastAPI copies an included router's routes into ``app.routes``; newer
versions (0.14x) keep a lazy ``_IncludedRouter`` wrapper holding the original
router instead. Walk both shapes.
"""


def iter_routes(routes):
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from iter_routes(original.routes)
        else:
            yield route


def find_route(app, path):
    """The API route registered at `path` (KeyError if none)."""
    for route in iter_routes(app.routes):
        if getattr(route, "path", None) == path and hasattr(route, "dependant"):
            return route
    raise KeyError(path)


def route_dependencies(app, path):
    """Dependency callables declared on the route registered at `path`."""
    return [d.call for d in find_route(app, path).dependant.dependencies]
