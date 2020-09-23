import starlette
from starlette.middleware import Middleware
from ddtrace import config
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w
from ddtrace.contrib.asgi.middleware import TraceMiddleware
from ddtrace.utils.wrappers import unwrap as _u
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)

config._add("starlette", dict(service_name=config._get_service(default="starlette"), distributed_tracing=True))


def patch():
    if getattr(starlette, "_datadog_patch", False):
        return

    setattr(starlette, "_datadog_patch", True)
    _w("starlette.applications", "Starlette.__init__", traced_init)


def unpatch():
    if getattr(starlette, "_datadog_patch", False):
        return

    setattr(starlette, "_datadog_patch", False)

    _u("starlette.applications", "Starlette.__init__")


def traced_init(wrapped, instance, args, kwargs):
    # FIXME: we'll need to set the config.asgi values based on what's been set for config.starlette
    # what we want is config.asgi["service_name"] to inherit from config.starlette

    mw = kwargs.pop("middleware", [])
    mw.insert(0, Middleware(TraceMiddleware))
    kwargs.update({"middleware": mw})

    wrapped(*args, **kwargs)
