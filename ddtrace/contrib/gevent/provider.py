import gevent

from ...provider import BaseContextProvider


# Greenlet attribute used to set/get the context
CONTEXT_ATTR = "__datadog_context"


class GeventContextProvider(BaseContextProvider):
    """Manages the active context for gevent execution.

    This provider depends on corresponding monkey patches to copy the active
    context from one greenlet to another.
    """

    def _get_current_context(self):
        """Helper to get the active context from the current greenlet."""
        current_g = gevent.getcurrent()
        if current_g is not None:
            return getattr(current_g, CONTEXT_ATTR, None)
        return None

    def _has_active_context(self):
        """Helper to determine if there is an active context."""
        return self._get_current_context() is not None

    def activate(self, context):
        """Sets the active context for the current running ``Greenlet``."""
        current_g = gevent.getcurrent()
        if current_g is not None:
            setattr(current_g, CONTEXT_ATTR, context)
            return context

    def active(self):
        """Returns the active context for this execution flow."""
        ctx = self._get_current_context()
        return ctx
