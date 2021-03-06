import functools
import logging
import json
from os import environ, getpid
import sys
import threading
from typing import Dict, List, Optional, Tuple, Union

from ddtrace.vendor import attr
from ddtrace.vendor import debtcollector

from .constants import (
    FILTERS_KEY,
    SAMPLING_PRIORITY_KEY,
    SAMPLE_RATE_METRIC_KEY,
    VERSION_KEY,
    ENV_KEY,
    ORIGIN_KEY,
    HOSTNAME_KEY,
)
from .context import Context
from .ext import system, SpanTypes
from .ext.priority import AUTO_REJECT, AUTO_KEEP
from .internal import debug, hostname
from .internal.logger import get_logger, hasHandlers
from .internal.runtime import RuntimeTags, RuntimeWorker, get_runtime_id
from .internal.writer import AgentWriter, LogWriter
from .internal import _rand
from .provider import DefaultContextProvider
from .sampler import DatadogSampler, RateSampler, RateByServiceSampler
from .settings import config
from .span import Span
from .utils.formats import asbool, get_env
from .utils.deprecation import deprecated, RemovedInDDTrace10Warning
from .vendor.dogstatsd import DogStatsd
from . import compat
from . import _hooks


log = get_logger(__name__)
debug_mode = asbool(get_env("trace", "debug", default=False))
partial_flush_enabled = asbool(get_env("tracer", "partial_flush_enabled", default=True))
partial_flush_min_spans = int(get_env("tracer", "partial_flush_min_spans", default=500))

DD_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] {}- %(message)s".format(
    "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s"
    " dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] "
)
if debug_mode and not hasHandlers(log):
    if config.logs_injection:
        logging.basicConfig(level=logging.DEBUG, format=DD_LOG_FORMAT)
    else:
        logging.basicConfig(level=logging.DEBUG)


@attr.s()
class _Trace(object):
    """Maintains the state of a trace (a collection of spans).

    This data structure is required since there are data that exist on the meta
    level of a trace. Currently this data is passed to the Datadog backend
    through trace chunk roots (first span of a trace payload to the agent).

    Unfortunately due to this design, trace state has to be maintained within
    the client and hence this data structure.
    """

    trace_id = attr.ib(type=int)  # type: int
    """
    The sampling decision made for the trace. This should only be set by the
    library initially when the root span is created but for legacy reasons
    can be set with the `span.sampled` property for any span in the trace.
    """
    sampled = attr.ib(type=bool, default=True)  # type: bool
    """
    The sampling priority decision for the trace. This is calculated by the library
    when the root span of a trace is created but for legacy reasons can be set
    with the `span.context.sampling_priority` attribute.
    """
    sampling_priority = attr.ib(default=None)  # type: Optional[int]
    """
    The origin of the trace. This is used in distributed tracing to indicate
    the source of a request (eg. synthetics).
    """
    dd_origin = attr.ib(default=None)  # type: Optional[str]
    """Number of spans finished in the trace."""
    _num_finished = attr.ib(type=int, default=0)  # type: int
    _spans = attr.ib(default=attr.Factory(list))  # type: List[Span]
    _lock = attr.ib(type=threading.Lock, default=attr.Factory(threading.Lock))  # type: threading.Lock

    def __len__(self):
        with self._lock:
            return len(self._spans)

    @property
    def root_span(self):
        # type: () -> Optional[Span]
        """Returns the first span created in a trace."""
        with self._lock:
            return self._spans[0] if len(self._spans) else None

    def add_span(self, span):
        # type: (Span) -> None
        with self._lock:
            self._spans.append(span)

    def finish_span(self):
        # type: () -> Tuple[List[Span], bool, bool]
        with self._lock:
            self._num_finished += 1

            if self._num_finished == len(self._spans) or (
                partial_flush_enabled and self._num_finished >= partial_flush_min_spans
            ):
                finished_spans = [s for s in self._spans if s.finished]

                chunk_root = finished_spans[0]
                if chunk_root:
                    if self.sampling_priority is not None and self.sampled:
                        chunk_root.set_metric(SAMPLING_PRIORITY_KEY, self.sampling_priority)
                    if self.dd_origin:
                        chunk_root.meta[ORIGIN_KEY] = str(self.dd_origin)

                self._spans = [s for s in self._spans if not s.finished]
                self._num_finished -= len(finished_spans)
                return finished_spans, self.sampled, len(self._spans) == 0

            return [], False, False


def _parse_dogstatsd_url(url):
    if url is None:
        return

    # url can be either of the form `udp://<host>:<port>` or `unix://<path>`
    # also support without url scheme included
    if url.startswith("/"):
        url = "unix://" + url
    elif "://" not in url:
        url = "udp://" + url

    parsed = compat.parse.urlparse(url)

    if parsed.scheme == "unix":
        return dict(socket_path=parsed.path)
    elif parsed.scheme == "udp":
        return dict(host=parsed.hostname, port=parsed.port)
    else:
        raise ValueError("Unknown scheme `%s` for DogStatsD URL `{}`".format(parsed.scheme))


_INTERNAL_APPLICATION_SPAN_TYPES = ["custom", "template", "web", "worker"]


class Tracer(object):
    """
    Tracer is used to create, sample and submit spans that measure the
    execution time of sections of code.

    If you're running an application that will serve a single trace per thread,
    you can use the global tracer instance::

        from ddtrace import tracer
        trace = tracer.trace('app.request', 'web-server').finish()
    """

    _RUNTIME_METRICS_INTERVAL = 10

    DEFAULT_HOSTNAME = environ.get("DD_AGENT_HOST", environ.get("DATADOG_TRACE_AGENT_HOSTNAME", "localhost"))
    DEFAULT_PORT = int(environ.get("DD_TRACE_AGENT_PORT", 8126))
    DEFAULT_DOGSTATSD_PORT = int(get_env("dogstatsd", "port", default=8125))
    DEFAULT_DOGSTATSD_URL = get_env(
        "dogstatsd", "url", default="udp://{}:{}".format(DEFAULT_HOSTNAME, DEFAULT_DOGSTATSD_PORT)
    )
    DEFAULT_AGENT_URL = environ.get("DD_TRACE_AGENT_URL", "http://%s:%d" % (DEFAULT_HOSTNAME, DEFAULT_PORT))

    def __init__(self, url=None, dogstatsd_url=DEFAULT_DOGSTATSD_URL):
        """
        Create a new ``Tracer`` instance. A global tracer is already initialized
        for common usage, so there is no need to initialize your own ``Tracer``.

        :param url: The Datadog agent URL.
        :param url: The DogStatsD URL.
        """
        self.log = log
        self.sampler = None
        self.priority_sampler = None
        self._runtime_worker = None
        self._filters = []
        self._traces = {}  # type: Dict[int, _Trace]
        self._traces_lock = threading.Lock()

        uds_path = None
        https = None
        hostname = self.DEFAULT_HOSTNAME
        port = self.DEFAULT_PORT
        writer = None

        if self._is_agentless_environment() and url is None:
            writer = LogWriter()
        else:
            if url is None:
                url = self.DEFAULT_AGENT_URL
            url_parsed = compat.parse.urlparse(url)
            if url_parsed.scheme in ("http", "https"):
                hostname = url_parsed.hostname
                port = url_parsed.port
                https = url_parsed.scheme == "https"
                # FIXME This is needed because of the way of configure() works right now, where it considers `port=None`
                # to be "no port set so let's use the default".
                # It should go away when we remove configure()
                if port is None:
                    if https:
                        port = 443
                    else:
                        port = 80
            elif url_parsed.scheme == "unix":
                uds_path = url_parsed.path
            else:
                raise ValueError("Unknown scheme `%s` for agent URL" % url_parsed.scheme)

        # globally set tags
        self.tags = config.tags.copy()

        # a buffer for service info so we don't perpetually send the same things
        self._services = set()

        # Runtime id used for associating data collected during runtime to
        # traces
        self._pid = getpid()

        self.enabled = asbool(get_env("trace", "enabled", default=True))

        # Apply the default configuration
        self.configure(
            hostname=hostname,
            port=port,
            https=https,
            uds_path=uds_path,
            sampler=DatadogSampler(),
            context_provider=DefaultContextProvider(),
            dogstatsd_url=dogstatsd_url,
            writer=writer,
        )

        self._hooks = _hooks.Hooks()

    def _get_trace(self, trace_id):
        # type: (int) -> Optional[_Trace]
        with self._traces_lock:
            return self._traces.get(trace_id, None)

    def _get_or_create_trace(self, trace_id):
        # type: (int) -> _Trace
        with self._traces_lock:
            if trace_id in self._traces:
                return self._traces[trace_id]
            else:
                trace = _Trace(trace_id=trace_id)
                self._traces[trace_id] = trace
                return trace

    def _is_sampled(self, span):
        # type: (Span) -> bool
        trace = self._get_trace(span.trace_id)
        return trace.sampled if trace else True

    def on_start_span(self, func):
        """Register a function to execute when a span start.

        Can be used as a decorator.

        :param func: The function to call when starting a span.
                     The started span will be passed as argument.
        """
        self._hooks.register(self.__class__.start_span, func)
        return func

    def deregister_on_start_span(self, func):
        """Unregister a function registered to execute when a span starts.

        Can be used as a decorator.

        :param func: The function to stop calling when starting a span.
        """

        self._hooks.deregister(self.__class__.start_span, func)
        return func

    @property
    def debug_logging(self):
        return self.log.isEnabledFor(logging.DEBUG)

    @debug_logging.setter
    @deprecated(message="Use logging.setLevel instead", version="1.0.0")
    def debug_logging(self, value):
        self.log.setLevel(logging.DEBUG if value else logging.WARN)

    @deprecated("Use .tracer, not .tracer()", "1.0.0")
    def __call__(self):
        return self

    @deprecated("This method will be removed altogether", "1.0.0")
    def global_excepthook(self, tp, value, traceback):
        """The global tracer except hook."""

    def _active_trace(self):
        # type: () -> Optional[_Trace]
        active = self.active()
        if active and active.trace_id:
            return self._get_trace(active.trace_id)
        return None

    def activate(self, span_or_ctx):
        # type: (Union[Context, Span]) -> None
        """Activate a span or context for the current execution context."""
        if isinstance(span_or_ctx, Context):
            trace = _Trace(
                trace_id=span_or_ctx.trace_id,
                sampling_priority=span_or_ctx.sampling_priority,
                dd_origin=span_or_ctx.dd_origin,
            )
            with self._traces_lock:
                self._traces[trace.trace_id] = trace
        self.context_provider.activate(span_or_ctx)

    def active(self):
        # type: () -> Optional[Union[Context, Span]]
        """Return the active span or context for the current execution context."""
        return self.context_provider.active()

    def active_span(self):
        # type: () -> Optional[Span]
        """Return the active span in the current execution context."""
        active = self.context_provider.active()
        return active if isinstance(active, Span) else None

    current_span = active_span

    def get_call_context(self, *args, **kwargs):
        # type (...) -> Context
        """Return the active ``Context`` for the current execution.

        This method makes use of a ``ContextProvider`` that is automatically set during the tracer
        initialization, or while using a library instrumentation.
        """
        active = self.context_provider.active(*args, **kwargs)
        if isinstance(active, Context):
            return active
        elif isinstance(active, Span):
            trace = self._get_trace(active.trace_id)
            return Context(
                span_id=active.span_id,
                trace_id=active.trace_id,
                sampling_priority=trace.sampling_priority if trace else None,
                dd_origin=trace.dd_origin if trace else None,
            )
        else:
            return Context()

    # TODO: deprecate this method and make sure users create a new tracer if they need different parameters
    @debtcollector.removals.removed_kwarg(
        "dogstatsd_host", "Use `dogstatsd_url` instead", category=RemovedInDDTrace10Warning
    )
    @debtcollector.removals.removed_kwarg(
        "dogstatsd_port", "Use `dogstatsd_url` instead", category=RemovedInDDTrace10Warning
    )
    def configure(
        self,
        enabled=None,
        hostname=None,
        port=None,
        uds_path=None,
        https=None,
        sampler=None,
        context_provider=None,
        wrap_executor=None,
        priority_sampling=None,
        settings=None,
        collect_metrics=None,
        dogstatsd_host=None,
        dogstatsd_port=None,
        dogstatsd_url=None,
        writer=None,
    ):
        """
        Configure an existing Tracer the easy way.
        Allow to configure or reconfigure a Tracer instance.

        :param bool enabled: If True, finished traces will be submitted to the API.
            Otherwise they'll be dropped.
        :param str hostname: Hostname running the Trace Agent
        :param int port: Port of the Trace Agent
        :param str uds_path: The Unix Domain Socket path of the agent.
        :param bool https: Whether to use HTTPS or HTTP.
        :param object sampler: A custom Sampler instance, locally deciding to totally drop the trace or not.
        :param object context_provider: The ``ContextProvider`` that will be used to retrieve
            automatically the current call context. This is an advanced option that usually
            doesn't need to be changed from the default value
        :param object wrap_executor: callable that is used when a function is decorated with
            ``Tracer.wrap()``. This is an advanced option that usually doesn't need to be changed
            from the default value
        :param priority_sampling: enable priority sampling, this is required for
            complete distributed tracing support. Enabled by default.
        :param collect_metrics: Whether to enable runtime metrics collection.
        :param str dogstatsd_host: Host for UDP connection to DogStatsD (deprecated: use dogstatsd_url)
        :param int dogstatsd_port: Port for UDP connection to DogStatsD (deprecated: use dogstatsd_url)
        :param str dogstatsd_url: URL for UDP or Unix socket connection to DogStatsD
        """
        if enabled is not None:
            self.enabled = enabled

        if settings is not None:
            filters = settings.get(FILTERS_KEY)
            if filters is not None:
                self._filters = filters

        # If priority sampling is not set or is True and no priority sampler is set yet
        if priority_sampling in (None, True) and not self.priority_sampler:
            self.priority_sampler = RateByServiceSampler()
        # Explicitly disable priority sampling
        elif priority_sampling is False:
            self.priority_sampler = None

        if sampler is not None:
            self.sampler = sampler

        if dogstatsd_host is not None and dogstatsd_url is None:
            dogstatsd_url = "udp://{}:{}".format(dogstatsd_host, dogstatsd_port or self.DEFAULT_DOGSTATSD_PORT)

        if dogstatsd_url is not None:
            dogstatsd_kwargs = _parse_dogstatsd_url(dogstatsd_url)
            self.log.debug("Connecting to DogStatsd(%s)", dogstatsd_url)
            self._dogstatsd_client = DogStatsd(**dogstatsd_kwargs)

        if writer:
            self.writer = writer
            self.writer.dogstatsd = self._dogstatsd_client
        elif (
            hostname is not None
            or port is not None
            or uds_path is not None
            or https is not None
            or priority_sampling is not None
            or sampler is not None
        ):
            # Preserve hostname and port when overriding priority sampling
            # This is clumsy and a good reason to get rid of this configure() API
            if hasattr(self, "writer") and isinstance(self.writer, AgentWriter):
                default_hostname = self.writer._hostname
                default_port = self.writer._port
                if https is None:
                    https = self.writer._https
            else:
                default_hostname = self.DEFAULT_HOSTNAME
                default_port = self.DEFAULT_PORT

            if hasattr(self, "writer") and self.writer.is_alive():
                self.writer.stop()

            self.writer = AgentWriter(
                hostname or default_hostname,
                port or default_port,
                uds_path=uds_path,
                https=https,
                sampler=self.sampler,
                priority_sampler=self.priority_sampler,
                dogstatsd=self._dogstatsd_client,
                report_metrics=config.health_metrics_enabled,
            )

        if context_provider is not None:
            self.context_provider = context_provider

        if wrap_executor is not None:
            self._wrap_executor = wrap_executor

        # Since we've recreated our dogstatsd agent, we need to restart metric collection with that new agent
        if self._runtime_worker:
            runtime_metrics_was_running = True
            self._runtime_worker.stop()
            self._runtime_worker.join()
            self._runtime_worker = None
        else:
            runtime_metrics_was_running = False

        if (collect_metrics is None and runtime_metrics_was_running) or collect_metrics:
            self._start_runtime_worker()

        if debug_mode or asbool(environ.get("DD_TRACE_STARTUP_LOGS", False)):
            try:
                info = debug.collect(self)
            except Exception as e:
                msg = "Failed to collect start-up logs: %s" % e
                self._log_compat(logging.WARNING, "- DATADOG TRACER DIAGNOSTIC - %s" % msg)
            else:
                if self.log.isEnabledFor(logging.INFO):
                    msg = "- DATADOG TRACER CONFIGURATION - %s" % json.dumps(info)
                    self._log_compat(logging.INFO, msg)

                # Always log errors since we're either in debug_mode or start up logs
                # are enabled.
                agent_error = info.get("agent_error")
                if agent_error:
                    msg = "- DATADOG TRACER DIAGNOSTIC - %s" % agent_error
                    self._log_compat(logging.WARNING, msg)

    def start_span(self, name, child_of=None, service=None, resource=None, span_type=None, activate=True):
        # type: (str, Optional[Union[Context, Span]], Optional[str], Optional[str], Optional[SpanTypes], bool) -> Span
        """Return a span that represents a unit of work designated by `name`.

        :param str name: the name of the operation being traced.
        :param object child_of: a ``Span`` or a ``Context`` instance representing the parent for this span.
        :param str service: the name of the service being traced.
        :param str resource: an optional name of the resource being tracked.
        :param str span_type: an optional operation type.
        :param activate: Whether or not to activate the span once it is created.

        To start a new root span, simply::

            span = tracer.start_span('web.request')
            span.finish()

        If you want to create a child for a root span, just::

            root_span = tracer.start_span('web.request')
            span = tracer.start_span('web.decoder', child_of=root_span)
            span.finish()

        Be sure to finish all spans to avoid memory leaks and incorrect
        parenting of spans.
        """
        self._check_new_process()

        if child_of is None:
            trace_id = parent_id = parent = None
        else:
            trace_id = child_of.trace_id
            parent_id = child_of.span_id
            parent = child_of if isinstance(child_of, Span) else None  # type: Optional[Span]

        # The following precedence is used for a new span's service:
        # 1. Explicitly provided service name
        #     a. User provided or integration provided service name
        # 2. Parent's service name (if defined)
        # 3. Globally configured service name
        #     a. `config.service`/`DD_SERVICE`/`DD_TAGS`
        if service is None:
            if parent:
                service = parent.service
            else:
                service = config.service

        if trace_id:
            # child_of a non-empty context, so either a local child span or from a remote context
            span = Span(
                self,
                name,
                trace_id=trace_id,
                parent_id=parent_id,
                service=service,
                resource=resource,
                span_type=span_type,
                _check_pid=False,
            )

            # Extra attributes when from a local parent
            if parent:
                span._parent = parent

            trace = self._get_or_create_trace(span.trace_id)
            trace.add_span(span)
            # If the parent object has a sampling priority or origin defined
            # then update the trace accordingly.
            sampling_priority = getattr(child_of, "sampling_priority", None)
            if sampling_priority is not None:
                trace.sampling_priority = sampling_priority
            dd_origin = getattr(child_of, "dd_origin", None)
            if dd_origin is not None:
                trace.dd_origin = dd_origin
        else:
            # this is the root span of a new trace
            span = Span(
                self,
                name,
                service=service,
                resource=resource,
                span_type=span_type,
                _check_pid=False,
            )
            span.metrics[system.PID] = self._pid or getpid()
            span.meta["runtime-id"] = get_runtime_id()
            if config.report_hostname:
                span.meta[HOSTNAME_KEY] = hostname.get_hostname()
            # add tags to root span to correlate trace with runtime metrics
            # only applied to spans with types that are internal to applications
            if self._runtime_worker and self._is_span_internal(span):
                span.meta["language"] = "python"

            sampled = self.sampler.sample(span)
            sampling_priority = None
            # Old behavior
            # DEV: The new sampler sets metrics and priority sampling on the span for us
            if not isinstance(self.sampler, DatadogSampler):
                if sampled:
                    # When doing client sampling in the client, keep the sample rate so that we can
                    # scale up statistics in the next steps of the pipeline.
                    if isinstance(self.sampler, RateSampler):
                        span.set_metric(SAMPLE_RATE_METRIC_KEY, self.sampler.sample_rate)

                    if self.priority_sampler:
                        # At this stage, it's important to have the service set. If unset,
                        # priority sampler will use the default sampling rate, which might
                        # lead to oversampling (that is, dropping too many traces).
                        if self.priority_sampler.sample(span):
                            sampling_priority = AUTO_KEEP
                        else:
                            sampling_priority = AUTO_REJECT
                else:
                    if self.priority_sampler:
                        # If dropped by the local sampler, distributed instrumentation can drop it too.
                        sampling_priority = AUTO_REJECT
            else:
                sampling_priority = AUTO_KEEP if sampled else AUTO_REJECT
                # The trace must be marked as sampled so it is forwarded to the agent.
                sampled = True

            trace = self._get_or_create_trace(span.trace_id)
            trace.sampled = sampled
            trace.sampling_priority = sampling_priority
            trace.add_span(span)

        if activate:
            self.context_provider.activate(span)

        # Apply default global tags.
        if self.tags:
            span.set_tags(self.tags)

        if config.env:
            span._set_str_tag(ENV_KEY, config.env)

        # Only set the version tag on internal spans.
        if config.version:
            root_span = self.active_root_span()
            # if: 1. the span is the root span and the span's service matches the global config; or
            #     2. the span is not the root, but the root span's service matches the span's service
            #        and the root span has a version tag
            # then the span belongs to the user application and so set the version tag
            if (root_span is None and service == config.service) or (
                root_span and root_span.service == service and VERSION_KEY in root_span.meta
            ):
                span._set_str_tag(VERSION_KEY, config.version)

        # update set of services handled by tracer
        if service and service not in self._services and self._is_span_internal(span):
            self._services.add(service)

            # The constant tags for the dogstatsd client needs to updated with any new
            # service(s) that may have been added.
            self._update_dogstatsd_constant_tags()

        self._hooks.emit(self.__class__.start_span, span)
        return span

    def _finish_span(self, span):
        # type: (Span) -> None

        # Only set the next active span to the parent if the span is active
        # and the parent is not finished.
        active = self.active()
        if active is span and span._parent and not span._parent.finished:
            self.context_provider.activate(span._parent)
        # Else if the span is the active span and there is no suitable parent,
        # activate nothing so future spans do not inherit from it.
        elif active is span:
            self.context_provider.activate(None)

        # It's possible that a span can be created from a trace that has already
        # been finished and deleted. So _get_or_create_trace has to be used instead
        # of _get_trace.
        trace = self._get_or_create_trace(span.trace_id)
        spans, sampled, trace_done = trace.finish_span()

        if trace_done:
            with self._traces_lock:
                del self._traces[trace.trace_id]

        if spans and sampled:
            self.write(spans)

    def _update_dogstatsd_constant_tags(self):
        """Prepare runtime tags for ddstatsd."""
        # DEV: ddstatsd expects tags in the form ['key1:value1', 'key2:value2', ...]
        tags = ["{}:{}".format(k, v) for k, v in RuntimeTags()]
        self.log.debug("Updating constant tags %s", tags)
        self._dogstatsd_client.constant_tags = tags

    def _start_runtime_worker(self):
        self._runtime_worker = RuntimeWorker(self._dogstatsd_client, self._RUNTIME_METRICS_INTERVAL)
        self._runtime_worker.start()

    def _check_new_process(self):
        """Checks if the tracer is in a new process (was forked) and performs
        the necessary updates if it is a new process
        """
        pid = getpid()
        if self._pid == pid:
            return

        self._pid = pid

        # We have to reseed the RNG or we will get collisions between the processes as
        # they will share the seed and generate the same random numbers.
        _rand.seed()

        # The previous process is responsible for flushing the spans it created.
        for trace_id, trace in self._traces:
            # Note that the remaining metadata is left in place so that
            # sampling decisions are not affected.
            trace.spans = []

        # Assume that the services of the child are not necessarily a subset of those
        # of the parent.
        self._services = set()

        if self._runtime_worker is not None:
            self._start_runtime_worker()

        # force an immediate update constant tags since we have reset services
        # and generated a new runtime id
        self._update_dogstatsd_constant_tags()

        # Re-create the background writer thread
        self.writer = self.writer.recreate()

    def _log_compat(self, level, msg):
        """Logs a message for the given level.

        Python 2 will not submit logs to stderr if no handler is configured.

        Instead, something like this will be printed to stderr:
            No handlers could be found for logger "ddtrace.tracer"

        Since the global tracer is configured on import and it is recommended
        to import the tracer as early as possible, it will likely be the case
        that there are no handlers installed yet.
        """
        if compat.PY2 and not hasHandlers(self.log):
            sys.stderr.write("%s\n" % msg)
        else:
            self.log.log(level, msg)

    def trace(self, name, service=None, resource=None, span_type=None):
        # type: (str, Optional[str], Optional[str], Optional[SpanTypes], bool) -> Span
        """Return an active span that inherits from the active span.

        The returned span will automatically be activated in the current
        execution and will inherit from the previously active span or context.

        :param str name: the name of the operation being traced
        :param str service: the name of the service being traced. If not set,
                            it will inherit the service from its parent.
        :param str resource: an optional name of the resource being tracked.
        :param str span_type: an optional operation type.

        The returned span *must* be `finish`'d or it will remain in memory
        indefinitely::

            >>> span = tracer.trace('web.request')
                try:
                    # do something
                finally:
                    span.finish()

            >>> with tracer.trace('web.request') as span:
                    # do something

        Example of the automatic parenting::

            parent = tracer.trace('parent')     # has no parent span, becomes the active span
            child  = tracer.trace('child')      # is a child of a parent, becomes the active span
            child.finish()
            parent.finish()

            parent2 = tracer.trace('parent2')   # has no parent span as parent and child have finished
            parent2.finish()
        """
        return self.start_span(
            name,
            child_of=self.active(),
            service=service,
            resource=resource,
            span_type=span_type,
            activate=True,
        )

    def active_root_span(self):
        # type: () -> Optional[Span]
        """Returns the root span of the current execution.

        This is useful for attaching information related to the trace as a
        whole without needing to add to child spans.

        For example::

            # get the root span
            root_span = tracer.active_root_span()
            # set the host just once on the root span
            if root_span:
                root_span.set_tag('host', '127.0.0.1')
        """
        trace = self._active_trace()
        return trace.root_span if trace else None

    current_root_span = active_root_span

    def write(self, spans):
        """
        Send the trace to the writer to enqueue the spans list in the agent
        sending queue.
        """
        if not spans:
            return  # nothing to do

        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug("writing %s spans (enabled:%s)", len(spans), self.enabled)
            for span in spans:
                self.log.debug("\n%s", span.pprint())

        if self.enabled and self.writer:
            for filtr in self._filters:
                try:
                    spans = filtr.process_trace(spans)
                except Exception:
                    log.error("error while applying filter %s to traces", filtr, exc_info=True)
                else:
                    if not spans:
                        return

            self.writer.write(spans=spans)

    @deprecated(message="Manually setting service info is no longer necessary", version="1.0.0")
    def set_service_info(self, *args, **kwargs):
        """Set the information about the given service."""
        return

    def wrap(self, name=None, service=None, resource=None, span_type=None):
        """
        A decorator used to trace an entire function. If the traced function
        is a coroutine, it traces the coroutine execution when is awaited.
        If a ``wrap_executor`` callable has been provided in the ``Tracer.configure()``
        method, it will be called instead of the default one when the function
        decorator is invoked.

        :param str name: the name of the operation being traced. If not set,
                         defaults to the fully qualified function name.
        :param str service: the name of the service being traced. If not set,
                            it will inherit the service from it's parent.
        :param str resource: an optional name of the resource being tracked.
        :param str span_type: an optional operation type.

        >>> @tracer.wrap('my.wrapped.function', service='my.service')
            def run():
                return 'run'

        >>> # name will default to 'execute' if unset
            @tracer.wrap()
            def execute():
                return 'executed'

        >>> # or use it in asyncio coroutines
            @tracer.wrap()
            async def coroutine():
                return 'executed'

        >>> @tracer.wrap()
            @asyncio.coroutine
            def coroutine():
                return 'executed'

        You can access the current span using `tracer.current_span()` to set
        tags:

        >>> @tracer.wrap()
            def execute():
                span = tracer.current_span()
                span.set_tag('a', 'b')
        """

        def wrap_decorator(f):
            # FIXME[matt] include the class name for methods.
            span_name = name if name else "%s.%s" % (f.__module__, f.__name__)

            # detect if the the given function is a coroutine to use the
            # right decorator; this initial check ensures that the
            # evaluation is done only once for each @tracer.wrap
            if compat.iscoroutinefunction(f):
                # call the async factory that creates a tracing decorator capable
                # to await the coroutine execution before finishing the span. This
                # code is used for compatibility reasons to prevent Syntax errors
                # in Python 2
                func_wrapper = compat.make_async_decorator(
                    self,
                    f,
                    span_name,
                    service=service,
                    resource=resource,
                    span_type=span_type,
                )
            else:

                @functools.wraps(f)
                def func_wrapper(*args, **kwargs):
                    # if a wrap executor has been configured, it is used instead
                    # of the default tracing function
                    if getattr(self, "_wrap_executor", None):
                        return self._wrap_executor(
                            self,
                            f,
                            args,
                            kwargs,
                            span_name,
                            service=service,
                            resource=resource,
                            span_type=span_type,
                        )

                    # otherwise fallback to a default tracing
                    with self.trace(span_name, service=service, resource=resource, span_type=span_type):
                        return f(*args, **kwargs)

            return func_wrapper

        return wrap_decorator

    def set_tags(self, tags):
        """Set some tags at the tracer level.
        This will append those tags to each span created by the tracer.

        :param dict tags: dict of tags to set at tracer level
        """
        self.tags.update(tags)

    def shutdown(self, timeout=None):
        """Shutdown the tracer.

        This will stop the background writer/worker and flush any finished traces in the buffer.

        :param timeout: How long in seconds to wait for the background worker to flush traces
            before exiting or :obj:`None` to block until flushing has successfully completed (default: :obj:`None`)
        :type timeout: :obj:`int` | :obj:`float` | :obj:`None`
        """
        if not self.writer.is_alive():
            return

        self.writer.stop()
        self.writer.join(timeout=timeout)

    @staticmethod
    def _is_agentless_environment():
        if (
            environ.get("DD_AGENT_HOST")
            or environ.get("DATADOG_TRACE_AGENT_HOSTNAME")
            or environ.get("DD_TRACE_AGENT_URL")
        ):
            # If one of these variables are set, we definitely have an agent
            return False
        if environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            # We are in an AWS Lambda environment
            return True
        return False

    @staticmethod
    def _is_span_internal(span):
        return not span.span_type or span.span_type in _INTERNAL_APPLICATION_SPAN_TYPES
