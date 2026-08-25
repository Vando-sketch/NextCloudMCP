"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, timedelta, timezone
from time import monotonic, sleep
from typing import Any, NamedTuple, TypeVar
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from caldav.collection import Calendar as DAVCalendar
from caldav.collection import Principal as DAVPrincipal
from caldav.davclient import DAVClient
from caldav.elements import dav
from caldav.elements import ical as ical_elements
from caldav.lib import error as caldav_error
from icalendar import Calendar, Event, Timezone, Todo
from lxml import etree

# caldav's top-level `caldav.DAVClient`/`DAVPrincipal`/`DAVCalendar`
# are exposed via PEP 562 module-level lazy imports (see caldav/__init__.py),
# which mypy cannot resolve as concrete classes usable in annotations
# ("Variable is not valid as a type"). Importing the same classes directly
# from their defining submodules sidesteps that - same runtime objects,
# just statically resolvable.

try:
    # caldav 3.x uses niquests (a requests-API-compatible client) by default,
    # falling back to requests if niquests isn't installed.
    from niquests import exceptions as _http_errors
except ImportError:  # pragma: no cover - depends on caldav's installed backend
    from requests import exceptions as _http_errors  # type: ignore[no-redef]

from . import event_mapping, mapping
from .errors import (
    AuthenticationFailedError,
    CalendarAlreadyExistsError,
    CalendarNotFoundError,
    ConnectionFailedError,
    EventNotFoundError,
    InvalidEventDataError,
    InvalidIcsDataError,
    InvalidTaskDataError,
    ObjectMoveError,
    TaskConflictError,
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
    TransientServerError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class _TaskLink(NamedTuple):
    """One task's place in its list's hierarchy, as `_subtask_links` reads it.

    `parent` is the UID its `RELATED-TO;RELTYPE=PARENT` names (None for a
    top-level task); `titel` is its SUMMARY, carried along so that reporting a
    dangling link doesn't cost a second fetch per task.
    """

    parent: str | None
    titel: str


# Nextcloud stores the calendar color as "#RRGGBB" or "#RRGGBBAA" (the Apple
# calendar-color extension property). Anything else is rejected up front so a
# typo can't end up as an unparseable property on the server.
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")

# Fallback bounds for one-sided time-range queries: CalDAV time-range filters
# technically allow an open side, but caldav's search() + expand handling is
# only well-defined with both ends present, so an omitted bound is widened to
# a range that comfortably covers any real-world calendar instead.
_RANGE_MIN = datetime(1901, 1, 1, tzinfo=timezone.utc)
_RANGE_MAX = datetime(2100, 1, 1, tzinfo=timezone.utc)

# How long the process-wide collection caches (`_collections`,
# `_collection_meta`) are served before the next access re-fetches them from
# Nextcloud, even without any self-made change to invalidate them. Both are
# otherwise cached for the life of the `CalDavService` (see their docstrings)
# and invalidated immediately when *this* process creates/renames/deletes a
# collection - but a rename or delete made through the Nextcloud web UI (or
# any other client) is invisible to that invalidation, so without a ceiling
# this process would keep resolving names against a collection list that no
# longer matches the server, indefinitely. One minute bounds how long such an
# out-of-band change can go unnoticed while still keeping the common case
# (several tool calls in one burst) cheap - except during a `get_agenda` call
# in flight, which freezes expiry for its own duration so its events and
# tasks are read from one consistent snapshot instead of two (see
# `_ttl_frozen`). The real worst-case bound is therefore this TTL *plus* the
# duration of the slowest `get_agenda` call overlapping it, not a flat one
# minute.
_COLLECTION_CACHE_TTL_SECONDS = 60.0

# How far the daylight-saving transitions written into an attached VTIMEZONE
# reach (see `_sync_vtimezones`). Matches `_RANGE_MAX`.
_VTIMEZONE_HORIZON = _RANGE_MAX.date()

# One tool call should stay one bounded unit of work: a caller asking to touch
# thousands of events is better served by several calls it can check in between.
_BATCH_UID_LIMIT = 200

# HTTP statuses that say "the request never reached a decision" rather than
# "the answer is no": a reverse proxy in front of Nextcloud timing out or
# dropping the upstream connection. Retrying one of these is the cheapest way
# to turn a batch that half-worked into one that worked. 4xx is excluded (the
# request itself is wrong, so a retry changes nothing) and so is 500, which
# Nextcloud returns for data it will keep refusing. 429 and 503-with-Retry-After
# never get here: caldav's own backoff (`rate_limit_handle`, see `__init__`)
# already sleeps and retries those, and its RateLimitError only surfaces once
# that gave up - a 503 *without* a Retry-After header falls through to us,
# hence its presence in this set.
_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})

# Three attempts with 0.5s then 1.0s in between: enough for a proxy hiccup or a
# restarting php-fpm worker to clear, short enough that 200 UIDs all failing
# can't stretch one tool call past a client's patience.
_BATCH_RETRY_ATTEMPTS = 3
_BATCH_RETRY_BASE_DELAY = 0.5

# caldav renders a failed response as "<status> <reason>\n\n<body>" (see
# `caldav.lib.error.errmsg`), so a status is only ever the leading token. The
# reason phrase has to be there too: the same field otherwise holds a real URL
# on some caldav error paths, and a host whose name begins with three digits
# must not read as a status code.
_LEADING_HTTP_STATUS_RE = re.compile(r"^\s*([1-5]\d{2})[ \t]+\S")

# A date-only string is what makes an event all-day (see `parse_datetime_input`).
_ALL_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Far enough in the past that no realistic `ende` can land before it.
_PROBE_START = datetime(1970, 1, 1, tzinfo=timezone.utc)

# The two supported task<->event link semantics, mapped to the RELATED-TO
# RELTYPE written on the *event* (never on the task - a RELATED-TO added to a
# VTODO would make Nextcloud Tasks render the task as a subtask of a
# non-task, garbling its UI; the event side has no such interpretation):
#   "zeitblock":      the event reserves time for the task (event = child).
#   "voraussetzung":  the event must happen before the task (event = parent).
_LINK_RELTYPES: dict[str, str] = {"zeitblock": "PARENT", "voraussetzung": "CHILD"}

# The single field name `move_task`/`move_event` accept in their `clear`
# argument. Moving an object between collections almost always changes its
# hierarchy too (a subtask's parent stays behind in the old list), so both
# move calls carry that one field as a shortcut - but they stay move tools,
# not general-purpose update tools, so the rest of `mapping._CLEAR_SPECS` /
# `event_mapping._CLEAR_SPECS` is deliberately not reachable through them.
_MOVE_TASK_CLEARABLE = "uebergeordnete_aufgabe"
_MOVE_EVENT_CLEARABLE = "verknuepfte_aufgabe"

# Runs of anything that isn't an ASCII letter/digit collapse to a single
# hyphen, so "Groceries & Errands!" -> "groceries-errands" (leading/trailing
# hyphens stripped separately, below).
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def _validate_move_clear(
    clear: tuple[str, ...] | list[str],
    allowed: str,
    value: str | None,
    error: type[TaskMcpError],
) -> bool:
    """Validate a move call's `clear` argument and report whether it clears `allowed`.

    Mirrors `mapping._validate_clear` / `event_mapping._validate_clear` down to
    the wording, but over the single field a move accepts: anything else is
    rejected by name (pointing at the update tool that does accept it), and
    naming the same field that this call also sets is the same contradiction
    the update tools reject.
    """
    names = tuple(clear or ())
    unknown = sorted({name for name in names if name != allowed})
    if unknown:
        raise error(
            f"Unknown felder_leeren entry/entries: {', '.join(unknown)}. "
            f"Expected one of: {allowed}."
        )
    if names and value is not None:
        raise error(f"Cannot both set and clear the same field in one call: {allowed}.")
    return bool(names)


def _slugify(display_name: str) -> str:
    """Derive a URL-safe CalDAV collection id from a task list's display name.

    Lowercases, collapses runs of non-alphanumeric characters to a single
    hyphen, and strips leading/trailing hyphens. Falls back to a random id
    if that leaves nothing usable (e.g. a name that's all emoji/CJK/etc. -
    non-ASCII scripts have no case-folded alphanumeric equivalent here).
    """
    slug = _SLUG_INVALID_CHARS.sub("-", display_name.strip().lower()).strip("-")
    return slug or f"list-{uuid.uuid4().hex[:8]}"


def _translate(exc: Exception) -> TaskMcpError:
    """Convert a caldav/requests exception into a clean, user-facing TaskMcpError.

    Messages returned here are forwarded verbatim to MCP clients (see
    `server.py`'s `_call`), so they must never embed raw exception text that
    could leak library/server internals (D7). Branches below that can't be
    reduced to an already-safe, specific message log the real exception
    server-side (`exc_info=True`) and return a categorized generic message
    instead.
    """
    if isinstance(exc, caldav_error.AuthorizationError):
        # caldav's own request layer collapses both HTTP 401 and 403 into this
        # one exception class before any response body is available to us (see
        # `DAVClient._sync_request`), so the server's specific `s:message`
        # (e.g. "Calendar limit reached") can't be recovered here - only the
        # HTTP reason phrase survives, in `.reason`. That phrase still reliably
        # tells 403 (Forbidden - a permission/quota problem) apart from 401
        # (Unauthorized - actually bad credentials), which is enough to stop
        # a 403 from being misreported as a credentials failure.
        if (exc.reason or "").strip().lower() == "forbidden":
            return TaskMcpError(
                "Nextcloud rejected the request as forbidden (HTTP 403). This is not "
                "a credentials problem - the account lacks permission for this "
                "operation, or a server-side limit (e.g. the calendar count limit) "
                "was reached."
            )
        return AuthenticationFailedError(
            "Nextcloud rejected the CalDAV credentials (check username/app password)."
        )
    if isinstance(exc, caldav_error.NotFoundError):
        return TaskMcpError("The requested resource was not found.")
    # Must be checked before the generic DAVError branch below, since
    # ETagMismatchError is a DAVError subclass (A4). caldav sends `If-Match`
    # and raises this on HTTP 412 when the task changed since it was last
    # read - that's an actionable, distinct condition, not a generic failure.
    if isinstance(exc, caldav_error.ETagMismatchError):
        return TaskConflictError(
            "The task was modified by another client since it was last read "
            "(conflicting edit). Re-fetch the task and retry."
        )
    # Also a DAVError subclass, so checked before the generic branch. caldav's
    # built-in backoff (rate_limit_handle, see __init__) retries 429/503
    # transparently; this error only surfaces once those retries are
    # exhausted, i.e. the server is enforcing a longer window. Nextcloud does
    # this by design for calendar *creation* (~10 new calendars per user per
    # hour), so the message names waiting as the fix rather than reading like
    # a server defect.
    if isinstance(exc, caldav_error.RateLimitError):
        return TaskMcpError(
            "Nextcloud is rate-limiting these requests (HTTP 429/503). This is "
            "expected after creating many calendars/task lists in a short time "
            "(Nextcloud allows roughly 10 new calendars per hour). Wait a while "
            "and retry."
        )
    if isinstance(exc, caldav_error.DAVError):
        # Checked before the flat "the request failed" below because the
        # difference is actionable: a gateway status means the request reached
        # no decision, which `_retry_transient` can act on - but only if this
        # translation preserves it. Flattening it here would silently turn
        # every retryable failure into a permanent one at the first call site
        # that translates before retrying.
        status = _dav_error_status(exc)
        if status in _TRANSIENT_HTTP_STATUSES:
            return TransientServerError(
                f"Nextcloud gave no answer to this request (HTTP {status}); it may or may "
                "not have been carried out. This is usually a proxy in front of Nextcloud "
                "rather than Nextcloud itself."
            )
        logger.warning("CalDAV request failed", exc_info=exc)
        return TaskMcpError("The CalDAV request failed on the Nextcloud server.")
    if isinstance(exc, (_http_errors.ConnectionError, _http_errors.Timeout)):
        return ConnectionFailedError(
            "Could not reach the Nextcloud server (connection refused or timed out)."
        )
    if isinstance(exc, _http_errors.RequestException):
        logger.warning("CalDAV network request failed", exc_info=exc)
        return ConnectionFailedError("A network error occurred talking to Nextcloud.")
    logger.warning("Unexpected error talking to Nextcloud", exc_info=exc)
    return TaskMcpError("An unexpected error occurred talking to Nextcloud.")


def _dav_error_status(exc: Exception) -> int | None:
    """Recover the HTTP status a caldav `DAVError` was built from, if it carries one.

    caldav keeps no field for it. `errmsg(response)` renders the response as
    "<status> <reason>\\n\\n<body>" and that string is handed to `DAVError` as
    its *url* argument (see `DAVObject._post_delete` and
    `CalendarObjectResource._post_put`), so the status survives only as the
    leading token of `.url`. Read defensively: anything that doesn't start with
    a three-digit number yields None, and every caller treats None as "not
    provably retryable".
    """
    for candidate in (getattr(exc, "url", None), getattr(exc, "reason", None)):
        if not isinstance(candidate, str):
            continue
        match = _LEADING_HTTP_STATUS_RE.match(candidate)
        if match:
            return int(match.group(1))
    return None


def _is_transient(exc: Exception) -> bool:
    """True for a failure that says nothing about the request, only about the moment."""
    if isinstance(exc, (TransientServerError, ConnectionFailedError)):
        # The already-translated forms. Most write paths run their exceptions
        # through `_translate` before anything else sees them, so recognizing
        # only the raw library exceptions below would leave the retries dead on
        # exactly the paths that need them most.
        return True
    if isinstance(exc, (_http_errors.ConnectionError, _http_errors.Timeout)):
        return True
    if isinstance(
        exc,
        (
            caldav_error.NotFoundError,
            caldav_error.AuthorizationError,
            caldav_error.ETagMismatchError,
            caldav_error.ScheduleTagMismatchError,
            caldav_error.ConsistencyError,
            caldav_error.RateLimitError,
        ),
    ):
        # Each of these is a definite answer about this request - and
        # RateLimitError only surfaces once caldav's own backoff has already
        # waited and given up, so retrying it here would only wait again.
        return False
    if isinstance(exc, caldav_error.DAVError):
        return _dav_error_status(exc) in _TRANSIENT_HTTP_STATUSES
    return False


def _sleep(seconds: float) -> None:
    """Indirection over `time.sleep` so tests can drive the retry path instantly."""
    sleep(seconds)


def _retry_transient(fn: Callable[[], _T]) -> _T:
    """Run `fn`, repeating it while it fails transiently.

    Each attempt is a whole operation - re-read the object, write it back - so
    a retry never replays a body built against a response that never arrived.
    That is also why this is only wrapped around batch items: the operations it
    covers are re-runnable, and a batch is where a proxy hiccup is expensive
    (the caller cannot retry "the third of thirteen moves" without first
    working out which of them went through).
    """
    delay = _BATCH_RETRY_BASE_DELAY
    for attempt in range(1, _BATCH_RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == _BATCH_RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            logger.info(
                "Transient CalDAV failure on attempt %s/%s, retrying",
                attempt,
                _BATCH_RETRY_ATTEMPTS,
                exc_info=exc,
            )
            _sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises


@dataclasses.dataclass(frozen=True)
class _BatchKind:
    """Everything that differs between the event batches and the task batches.

    The batch machinery itself (dedup, the UID limit, resolving the collection
    once, the partial-failure report) is identical for both, so it lives in
    `_batch_over_uids` and is handed one of the two instances below rather than
    being written twice.

    `component` is the CalDAV component kind the collection must support;
    `name_key` is what the returned envelope calls the collection, matching the
    parameter name the tool takes; `uids_param` and `noun` only appear in error
    messages, and `invalid_error` is the kind-specific `InvalidTaskDataError` /
    `InvalidEventDataError` used both for rejecting the UID list up front and
    for recognizing a patch that doesn't fit one particular object.
    """

    component: str
    name_key: str
    uids_param: str
    noun: str
    invalid_error: type[TaskMcpError]


_EVENT_BATCH = _BatchKind("VEVENT", "kalender_name", "event_uids", "Event", InvalidEventDataError)
_TASK_BATCH = _BatchKind("VTODO", "list_name", "task_uids", "Task", InvalidTaskDataError)


def _aborted_batch(
    cause: TaskMcpError,
    kind: _BatchKind,
    uid: str,
    results: list[dict[str, Any]],
    unique_uids: list[str],
) -> TaskMcpError:
    """Re-raise a call-scoped failure with what the batch had already done.

    Some failures are worth stopping for - the server is unreachable, the
    credentials are wrong, a write keeps failing however often it is retried.
    Continuing through 200 UIDs would then just produce 200 copies of the same
    error, slowly.

    But an exception carries no `ergebnisse`, and "which of the thirteen went
    through?" is the exact question a half-finished migration leaves behind. So
    the report is folded into the message: what stopped it, where, and which
    UIDs are still to do. The class is kept, so callers that distinguish e.g.
    an authentication failure still can.
    """
    # `results` has no entry for the UID that is failing right now, so the slice
    # starts at it: it and everything after it still needs doing.
    still_to_do = unique_uids[len(results) :]
    done = [entry["uid"] for entry in results if entry["status"] == "ok"]
    noun = kind.noun.lower()
    detail = f"{cause} Stopped at {noun} '{uid}': {len(done)} of {len(unique_uids)} were done"
    detail += f" ({', '.join(done)})." if done else "."
    detail += f" Still to do: {', '.join(still_to_do)}. Re-run with those once the cause is gone."
    return type(cause)(detail)


# ----------------------------------------------------------------------
# Nextcloud-specific DAV extensions (sharing, trashbin): these are not part
# of any CalDAV RFC and have no API in the caldav library, so the methods
# below build/parse raw XML and send it through `DAVClient.request` directly
# (see `CalDavService._dav_request`). Namespaces per Nextcloud's sabre/dav
# app (`OCA\DAV\DAV\Sharing\Plugin::NS_OWNCLOUD`/`NS_NEXTCLOUD`).
# ----------------------------------------------------------------------

_DAV_NS = "DAV:"
_OC_NS = "http://owncloud.org/ns"
_NC_NS = "http://nextcloud.com/ns"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
# Apple's calendar extension namespace, used only for the (cosmetic)
# calendar-color property Nextcloud stores under it.
_ICAL_NS = "http://apple.com/ns/ical/"

# Maps the {oc}invite-* child element found on an {oc}user share entry to
# the German status vocabulary this server returns. Nextcloud's own server
# currently always emits invite-accepted (shares are auto-accepted), but
# other invite-* elements are part of the same sharing XML vocabulary (they
# do appear for the closely related calendarserver.org CalDAV-sharing
# elements some clients/servers use) - handled liberally rather than assumed
# absent.
_INVITE_STATUS_MAP = {
    "invite-accepted": "akzeptiert",
    "invite-declined": "abgelehnt",
    "invite-noresponse": "ausstehend",
    "invite-invalid": "ungueltig",
    "invite-deleted": "geloescht",
}

# A trashbin object's resource name is "<numeric id>.ics" (see Nextcloud's
# DeletedCalendarObjectsCollection::getChild, which 404s on anything else).
_TRASH_ID_RE = re.compile(r"^\d+\.ics$")


def _clark(namespace: str, tag: str) -> str:
    """Build a Clark-notation tag ("{ns}tag") for lxml element construction/lookup."""
    return f"{{{namespace}}}{tag}"


def _local_name(tag: str) -> str:
    """Strip the Clark-notation namespace off an lxml element tag."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _principal_href(empfaenger: str, gruppe: bool) -> str:
    """Build the "principal:principals/<users|groups>/<id>" href Nextcloud's
    sharing plugin expects in a `{oc}share` request's `{DAV:}href`."""
    kind = "groups" if gruppe else "users"
    return f"principal:principals/{kind}/{empfaenger}"


def _parse_principal_href(href: str) -> tuple[str | None, str]:
    """Reverse of `_principal_href`: recover (id, "benutzer"|"gruppe") from a
    share entry's `{DAV:}href` text (as returned in a `{oc}invite` listing).

    Parsed liberally: the leading "principal:" scheme prefix is optional, and
    any href of the general shape ".../<users|groups>/<id>" is recognized
    even without a "principals/" segment before it, so a server that renders
    this slightly differently is still handled. Falls back to treating the
    last path segment as a user id if the kind can't be determined.
    """
    remainder = href.strip()
    if remainder.lower().startswith("principal:"):
        remainder = remainder[len("principal:") :]
    parts = [p for p in remainder.strip("/").split("/") if p]
    if not parts:
        return None, "benutzer"
    if len(parts) >= 2 and parts[-2] in ("users", "groups"):
        kind = "gruppe" if parts[-2] == "groups" else "benutzer"
        return unquote(parts[-1]), kind
    return unquote(parts[-1]), "benutzer"


def _share_request_body(principal_href: str, *, remove: bool, read_write: bool) -> str:
    """Build the `{oc}share` POST body to add/update or remove one sharee."""
    root = etree.Element(_clark(_OC_NS, "share"), nsmap={"d": _DAV_NS, "o": _OC_NS})
    if remove:
        action = etree.SubElement(root, _clark(_OC_NS, "remove"))
        etree.SubElement(action, _clark(_DAV_NS, "href")).text = principal_href
    else:
        action = etree.SubElement(root, _clark(_OC_NS, "set"))
        etree.SubElement(action, _clark(_DAV_NS, "href")).text = principal_href
        etree.SubElement(action, _clark(_OC_NS, "summary"))
        if read_write:
            etree.SubElement(action, _clark(_OC_NS, "read-write"))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def _invite_propfind_body() -> str:
    """Build a PROPFIND body requesting just the `{oc}invite` property."""
    root = etree.Element(_clark(_DAV_NS, "propfind"), nsmap={"d": _DAV_NS, "o": _OC_NS})
    prop = etree.SubElement(root, _clark(_DAV_NS, "prop"))
    etree.SubElement(prop, _clark(_OC_NS, "invite"))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def _trashbin_report_body() -> str:
    """Build a `CALDAV:calendar-query` REPORT body for listing trashbin
    objects (Nextcloud's calendar-trashbin plugin, namespace
    `http://nextcloud.com/ns`).

    A REPORT, not a PROPFIND: Nextcloud's `DeletedCalendarObjectsCollection`
    doesn't implement child listing, so a Depth-1 PROPFIND on `objects/` is
    answered with 501 Not Implemented - the collection only exposes its
    contents through calendar-query (issue #13).
    """
    root = etree.Element(
        _clark(_CALDAV_NS, "calendar-query"),
        nsmap={"d": _DAV_NS, "c": _CALDAV_NS, "nc": _NC_NS},
    )
    prop = etree.SubElement(root, _clark(_DAV_NS, "prop"))
    etree.SubElement(prop, _clark(_DAV_NS, "displayname"))
    etree.SubElement(prop, _clark(_CALDAV_NS, "calendar-data"))
    etree.SubElement(prop, _clark(_NC_NS, "deleted-at"))
    etree.SubElement(prop, _clark(_NC_NS, "calendar-uri"))
    filter_el = etree.SubElement(root, _clark(_CALDAV_NS, "filter"))
    etree.SubElement(filter_el, _clark(_CALDAV_NS, "comp-filter"), name="VCALENDAR")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def _collection_props_propfind_body() -> str:
    """PROPFIND body requesting the per-collection properties this bridge
    caches (resource type, display name, supported component set, color) for
    every calendar in one Depth-1 request over the calendar-home-set.

    caldav's own Depth-1 listing (`CALENDAR_LIST_PROPS`) already fetches this
    exact set but discards everything except the display name when it rebuilds
    its `Calendar` objects, so `get_supported_components()` and the
    calendar-color `get_properties()` each re-issue a PROPFIND *per calendar* -
    an O(N) round-trip cascade on every listing. Reading it once here and
    looking values up by href (see `_fetch_collection_meta`) collapses that
    back to a single request.
    """
    root = etree.Element(
        _clark(_DAV_NS, "propfind"),
        nsmap={"d": _DAV_NS, "c": _CALDAV_NS, "ical": _ICAL_NS},
    )
    prop = etree.SubElement(root, _clark(_DAV_NS, "prop"))
    etree.SubElement(prop, _clark(_DAV_NS, "resourcetype"))
    etree.SubElement(prop, _clark(_DAV_NS, "displayname"))
    etree.SubElement(prop, _clark(_CALDAV_NS, "supported-calendar-component-set"))
    etree.SubElement(prop, _clark(_ICAL_NS, "calendar-color"))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


def _normalize_collection_href(href: str) -> str:
    """Path-only, unquoted, trailing-slash form of a collection href.

    Lets an href from a raw multistatus response (`/remote.php/dav/...`) and a
    caldav `Calendar.url` (a full `https://.../` URL) compare equal regardless
    of absolute-vs-relative form or percent-encoding, so metadata parsed from
    the batch PROPFIND can be looked up by a calendar object's URL.
    """
    path = urlsplit(href).path or href
    path = unquote(path).strip()
    if not path.endswith("/"):
        path += "/"
    return path


def _parse_supported_components(prop_el: Any) -> set[str]:
    """Extract component names from a `supported-calendar-component-set`
    element (its `<C:comp name="VEVENT"/>` children).

    An empty set means the server advertised none, which per RFC 4791 §5.2.3
    means "supports all components" - callers treat it as fail-open.
    """
    components: set[str] = set()
    if prop_el is None:
        return components
    for child in prop_el:
        if _local_name(child.tag) == "comp":
            name = child.get("name")
            if name:
                components.add(name)
    return components


def _iter_multistatus_responses(
    tree: Any,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (href, {Clark-tag: element}) for each `{DAV:}response` in a
    multistatus tree, collecting only properties reported with a 200 OK
    propstat (a property Nextcloud doesn't have for a given resource comes
    back under a 404 propstat instead, which is silently skipped here).
    """
    if tree is None:
        return
    for response_el in tree.findall(_clark(_DAV_NS, "response")):
        href_el = response_el.find(_clark(_DAV_NS, "href"))
        if href_el is None or not href_el.text:
            continue
        props: dict[str, Any] = {}
        for propstat_el in response_el.findall(_clark(_DAV_NS, "propstat")):
            status_el = propstat_el.find(_clark(_DAV_NS, "status"))
            if status_el is None or status_el.text is None or " 200 " not in f" {status_el.text} ":
                continue
            prop_el = propstat_el.find(_clark(_DAV_NS, "prop"))
            if prop_el is None:
                continue
            for child in prop_el:
                props[child.tag] = child
        yield href_el.text, props


def _parse_invite_response(tree: Any) -> list[dict[str, Any]]:
    """Parse a `{oc}invite` PROPFIND response into share dicts."""
    shares: list[dict[str, Any]] = []
    for _, props in _iter_multistatus_responses(tree):
        invite_el = props.get(_clark(_OC_NS, "invite"))
        if invite_el is None:
            continue
        for user_el in invite_el:
            if _local_name(user_el.tag) != "user":
                continue  # skip {oc}organizer - that's the sharer, not a sharee
            href_el = user_el.find(_clark(_DAV_NS, "href"))
            if href_el is None or not href_el.text:
                continue
            empfaenger, typ = _parse_principal_href(href_el.text)
            if not empfaenger:
                continue
            access_el = user_el.find(_clark(_OC_NS, "access"))
            read_write = access_el is not None and any(
                _local_name(child.tag) == "read-write" for child in access_el
            )
            status = "akzeptiert"
            for child in user_el:
                local = _local_name(child.tag)
                if local.startswith("invite-"):
                    status = _INVITE_STATUS_MAP.get(local, local[len("invite-") :])
                    break
            shares.append(
                {
                    "empfaenger": empfaenger,
                    "typ": typ,
                    "schreibzugriff": read_write,
                    "status": status,
                }
            )
    return shares


def _parse_deleted_at(raw: str | None) -> str | None:
    """Parse a `{nc}deleted-at` property value into an ISO timestamp.

    Nextcloud's calendar-trashbin plugin has been observed to encode this as
    a raw Unix epoch integer (tried first) as well as an ISO 8601 string
    (`DateTimeInterface::ATOM`, e.g. from newer server versions) - both are
    accepted; anything else parses to None rather than raising, since this is
    a display-only field.

    This is the server's own record of when it deleted something, always
    UTC-based, so an ISO value that arrives *without* an offset is read as UTC
    rather than as a local wall clock - unlike caller input, and unlike the
    floating times foreign clients put in calendar components. It is then
    rendered in the default timezone like every other timestamp.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = datetime.fromtimestamp(int(text), tz=timezone.utc)
        return mapping.format_datetime_output(dt)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return mapping.format_datetime_output(dt)


def _derive_title_and_type(ics_text: str | None) -> tuple[str | None, str | None]:
    """Best-effort SUMMARY/component-kind extraction from a trashbin item's
    raw calendar-data, for `list_trash`'s "titel"/"typ" fields.

    Returns (None, None) if `ics_text` is missing or unparseable - a trashbin
    listing entry is still useful without a title.
    """
    if not ics_text or not ics_text.strip():
        return None, None
    try:
        parsed = Calendar.from_ical(ics_text)
    except Exception:
        return None, None
    for component in parsed.walk():
        if component.name == "VEVENT":
            summary = component.get("summary")
            return (str(summary) if summary else None), "termin"
        if component.name == "VTODO":
            summary = component.get("summary")
            return (str(summary) if summary else None), "aufgabe"
    return None, None


def _zone_preserving_isoformat(value: datetime) -> str:
    """Render a datetime so that parsing it back keeps its zone, not just its offset.

    `EventFields` takes strings, so anything handed to it internally
    (`create_event_from_task`) has to survive a second trip through
    `mapping.parse_datetime_input`. `isoformat()` writes a numeric offset,
    which names an instant but no zone - the event would end up pinned to a UTC
    instant instead of anchored to the zone its start was resolved in. The
    "<naive datetime> <IANA name>" form that same parser accepts keeps it.
    """
    zone = value.tzinfo
    if isinstance(zone, ZoneInfo):
        return f"{value.replace(tzinfo=None).isoformat()} {zone.key}"
    return value.isoformat()


def _dedup_strings(values: list[str] | None) -> list[str] | None:
    """Drop repeats, keeping the given order.

    A collection name listed twice would have its collection read twice and
    make `list_tags` count every tag in it twice; an event UID listed twice
    would be written or deleted twice and reported twice.
    """
    if values is None:
        return None
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _instance_markers(obj: Any, component: str) -> Counter[str] | None:
    """Count the `component` instances inside `obj`'s calendar object, per instance key.

    A recurring event or task is not one component but a master plus one
    VEVENT/VTODO per RECURRENCE-ID override, all inside the same VCALENDAR.
    A UID lookup can't tell those apart: it resolves as long as *any* of them
    survived a write. `_move_object` compares these markers before deleting a
    source, so a server that silently dropped override instances during the
    copy is caught instead of costing the original.

    A RECURRENCE-ID is reduced to the instant it names, not to its wire form:
    a server is free to store `TZID=Europe/Berlin:...T100000` as `...T080000Z`,
    and comparing the raw strings would then reject a copy that is in fact
    complete.

    Counts, not a set: two instances sharing a RECURRENCE-ID are two
    instances, and losing one of them must not look like losing nothing.

    Returns `None` only when the calendar object could not be read at all.
    An empty counter is a real answer - "this object holds no such component"
    - and the caller must treat it as a failed copy, not as "unknown".
    """
    try:
        subcomponents = list(obj.icalendar_instance.subcomponents)
    except Exception:
        return None
    markers: Counter[str] = Counter()
    for sub in subcomponents:
        if getattr(sub, "name", None) != component:
            continue
        recurrence_id = sub.get("recurrence-id")
        if recurrence_id is None:
            # The master instance; a non-recurring object has only this one.
            markers[""] += 1
            continue
        markers[_recurrence_marker(recurrence_id)] += 1
    return markers


def _recurrence_marker(prop: Any) -> str:
    """Reduce a RECURRENCE-ID property to a value comparable across servers.

    Everything datelike collapses to a UTC instant, so a server storing a
    DATE as midnight or a TZID as its UTC equivalent still matches.
    """
    value = getattr(prop, "dt", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Floating time: no instant to normalize to, compare as written.
            return value.strftime("%Y%m%dT%H%M%S")
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    try:
        return prop.to_ical().decode("utf-8", "replace")
    except Exception:
        return str(prop)


def _sync_vtimezones(vcal: Calendar, component: Any) -> None:
    """Ensure `vcal` has a VTIMEZONE for every IANA zone `component` uses.

    `event_mapping._parse_datetime` keeps an explicit IANA zone name (e.g.
    "Europe/Berlin") as a `zoneinfo.ZoneInfo`-aware datetime instead of
    collapsing it to a fixed UTC instant, so DTSTART/DTEND/EXDATE can come
    out on the wire as e.g. `DTSTART;TZID=Europe/Berlin:...`. Per RFC 5545
    3.6.5, a TZID referenced like that needs a matching VTIMEZONE component
    in the same VCALENDAR, or other clients can't resolve it - this builds
    one (via `icalendar.Timezone.from_tzinfo`) for each such zone and adds
    it to `vcal`, skipping any TZID `vcal` already has (mirroring
    `export_calendar`'s `seen_tzids` de-dup pattern).

    `icalendar` writes the zone's transitions as an explicit RDATE list rather
    than as a recurrence rule, and stops at 2038-01-01 unless told otherwise;
    a client reading such a component applies the last observance it finds to
    every later date, so a weekly event's summer occurrences would come out an
    hour off from 2038 on. The list is therefore generated up to
    `_VTIMEZONE_HORIZON` instead, at a cost of about 2 KB per written event -
    the same horizon `_RANGE_MAX` already treats as "far enough".
    """
    seen_tzids = {str(c.get("TZID", "")) for c in vcal.subcomponents if c.name == "VTIMEZONE"}
    zones: dict[str, ZoneInfo] = {}
    # "due" is the VTODO counterpart of "dtend": since finding 5.7,
    # `mapping.apply_task_fields` anchors DTSTART/DUE the way
    # `apply_event_fields` anchors DTSTART/DTEND, so a *task* can reference a
    # TZID here too and needs the same VTIMEZONE written alongside it.
    for prop_name in ("dtstart", "dtend", "due"):
        prop = component.get(prop_name)
        if prop is None:
            continue
        tzinfo = getattr(prop.dt, "tzinfo", None)
        if isinstance(tzinfo, ZoneInfo):
            zones.setdefault(tzinfo.key, tzinfo)
    exdate_prop = component.get("exdate")
    if exdate_prop is not None:
        for entry in exdate_prop if isinstance(exdate_prop, list) else [exdate_prop]:
            for dt_item in getattr(entry, "dts", []):
                tzinfo = getattr(dt_item.dt, "tzinfo", None)
                if isinstance(tzinfo, ZoneInfo):
                    zones.setdefault(tzinfo.key, tzinfo)

    for tzid, zone in zones.items():
        if tzid in seen_tzids:
            continue
        # Inserted at the front (not appended) so a VTIMEZONE always precedes
        # any VEVENT/VTODO already in `vcal` - required by RFC 5545 3.6.5,
        # and `update_event` syncs onto a component already carrying its
        # VEVENT, unlike `create_event`'s empty `vcal`.
        vcal.subcomponents.insert(
            0, Timezone.from_tzinfo(zone, tzid=tzid, last_date=_VTIMEZONE_HORIZON)
        )
        seen_tzids.add(tzid)


def _apply_task_patch(todo_obj: Any, fields: mapping.TaskFields) -> None:
    """Write `fields` onto a fetched task object and save it back.

    The patch goes on the *series master*, not on whatever component caldav
    happens to hand back as `icalendar_component`: a recurring task's file also
    holds its RECURRENCE-ID overrides, and patching one of those would change a
    single occurrence while the caller asked to change the task.

    Shared by `update_task` and `update_tasks` so the two cannot drift apart,
    and written to be re-runnable: every field here is set rather than
    appended, so a retry after a lost response lands on the same result.
    """
    master = None
    for component in todo_obj.icalendar_instance.walk("VTODO"):
        if "recurrence-id" not in component:
            master = component
            break
    if master is None:
        master = todo_obj.icalendar_component
    mapping.apply_task_fields(master, fields)
    _sync_vtimezones(todo_obj.icalendar_instance, master)
    todo_obj.save()


def _reject_occurrence_uid(task_uid: str) -> None:
    """Refuse a UID that names one expanded occurrence rather than a stored task.

    `list_tasks`/`get_agenda` expand a recurring task into the occurrences due
    inside the queried window (`mapping._expand_recurring_tasks`). Those rows
    are a read-only view of dates in a series - there is no stored task at their
    UID to edit, complete or delete, and this server has no way to materialize
    one occurrence as its own component.

    Every task tool that takes a UID checks this, so passing an instance's UID
    back fails with an explanation naming the series' own UID, instead of
    silently doing something to the *whole series* that the caller meant to do
    to one date (finding 5.1).
    """
    series_uid, occurrence = mapping.split_occurrence_uid(task_uid)
    if occurrence is None:
        return
    raise InvalidTaskDataError(
        f"'{task_uid}' identifies the occurrence on {occurrence} of a recurring task, "
        "not a task on its own - single occurrences cannot be edited, completed or "
        f"deleted. Use the series' own uid '{series_uid}' (reported as 'serie_uid' on "
        "every expanded instance) to act on the whole series, or add the occurrence to "
        "the series' ausnahme_daten to skip just that one."
    )


class CalDavService:
    """Holds one reused CalDAV connection and exposes task CRUD operations on it."""

    def __init__(self, url: str, username: str, password: str, timeout: int = 30) -> None:
        self._client = DAVClient(
            url=url,
            username=username,
            password=password,
            timeout=timeout,
            # Send Basic credentials pre-emptively instead of waiting for the
            # first request to bounce off a 401 and re-issuing it with auth.
            # Without `auth_type`, caldav starts with no auth object and only
            # builds one from the 401 challenge, so the very first request of
            # the process pays an extra round-trip; Nextcloud accepts Basic, so
            # naming it up front skips that (caldav's own docs recommend this
            # to reduce request count). One-time, not per-call.
            auth_type="basic",
            # A5: without this, caldav 3.2.1 raises RateLimitError immediately
            # on a 429/503 response instead of backing off - a transient,
            # server-side "slow down" turns into a hard failure for every
            # caller. `rate_limit_handle=True` makes it sleep (honoring the
            # server's Retry-After header when present, falling back to
            # `rate_limit_default_sleep` otherwise) and retry instead;
            # `rate_limit_max_sleep` caps how long any single wait can be, so
            # a server asking for an extreme Retry-After can't stall a tool
            # call indefinitely. These are caldav's own built-in retry
            # mechanism - deliberately not reimplemented here.
            rate_limit_handle=True,
            rate_limit_default_sleep=5,
            rate_limit_max_sleep=60,
        )
        self._username = username
        self._principal: DAVPrincipal | None = None
        # A1 moves CalDavService calls onto worker threads (via
        # anyio.to_thread.run_sync in server.py) so they no longer block the
        # asyncio event loop - but that means calls can now genuinely run
        # concurrently against the single shared DAVClient/HTTP session. This
        # lock serializes the actual CalDAV operations (folding in the old
        # principal-only lock) to keep that access correct; it intentionally
        # trades away parallel Nextcloud access for correctness, while still
        # keeping the event loop itself free to serve other requests. It also
        # guards `_calendar_cache` below (A3).
        self._lock = threading.RLock()
        # Resolving a calendar by display name costs a full PROPFIND + linear
        # scan (`principal.calendars()`) on every call. Cache resolved
        # calendars by (component, display name) so repeat calls for the same
        # name skip that round-trip entirely (A3). The component is part of
        # the key because Nextcloud keeps task lists (VTODO) and event
        # calendars (VEVENT) in the same DAV namespace, and the same display
        # name may legitimately exist once per kind. Guarded by `_lock`, like
        # everything else that touches CalDAV state.
        #
        # Entries carry the `monotonic()` reading they were cached at and
        # expire after `_COLLECTION_CACHE_TTL_SECONDS`, exactly like
        # `_collections`/`_collection_meta` below - and for a sharper reason
        # than those two. A cache *hit* here short-circuits resolution
        # entirely, so their TTL never gets consulted on this path; without
        # one of its own, a name whose collection changed identity
        # server-side would be answered from this dict forever. That is not
        # hypothetical: rename "CSGO" to something else in the Nextcloud web
        # UI and give a *different*, new collection that freed-up name, and
        # every explicit lookup of "CSGO" would keep hitting the old
        # collection - which still exists, so nothing 404s and the
        # `_with_collection` retry below never fires - serving its contents
        # under a name that now belongs to someone else. A plain deletion
        # does self-correct through that retry, but only because the request
        # fails; name reuse fails silently, which is the worse half.
        self._calendar_cache: dict[tuple[str, str], tuple[DAVCalendar, float]] = {}
        # Lazily discovered and cached like the calendar cache above (A3's
        # reasoning applies equally here): the caller's own address(es) don't
        # change during the lifetime of one CalDavService, so there is no
        # reason to re-run the principal PROPFIND(s) on every create_event
        # call that adds attendees. Guarded by `_lock`.
        self._own_organizer_address: str | None = None
        self._own_calendar_user_addresses: list[str] | None = None
        # Per-collection metadata (supported component set + color) keyed by
        # normalized collection href, fetched in ONE Depth-1 PROPFIND over the
        # calendar-home-set (`_fetch_collection_meta`) and cached for up to
        # `_COLLECTION_CACHE_TTL_SECONDS`. This replaces caldav's per-calendar
        # `get_supported_components()` / calendar-color `get_properties()`
        # calls, which each cost a full PROPFIND - an O(N) round-trip cascade
        # on every listing and every cold name resolution, which was the
        # dominant source of per-tool-call latency. Invalidated immediately
        # whenever the set of collections (or a color) changes below, and
        # re-fetched unconditionally once the TTL elapses even without such a
        # change (see `_COLLECTION_CACHE_TTL_SECONDS`). Guarded by `_lock`.
        self._collection_meta: dict[str, dict[str, Any]] | None = None
        # The resolved `principal.calendars()` list, cached for up to
        # `_COLLECTION_CACHE_TTL_SECONDS`. caldav re-runs home-set discovery
        # *and* the calendar-list PROPFIND (2 round-trips) on every
        # `.calendars()` call and never reuses them, so every listing/
        # resolution paid them afresh; `get_agenda` alone does it several
        # times. Invalidated together with the metadata above whenever a
        # collection is created/deleted/renamed by this process (or a cached
        # collection turns out stale mid-request), and re-fetched
        # unconditionally once the TTL elapses even without such a change -
        # this is what stops a collection renamed/deleted through the
        # Nextcloud web UI (invisible to this process's own invalidation)
        # from being served under a stale name/identity forever. Guarded by
        # `_lock`.
        self._collections: list[DAVCalendar] | None = None
        # `monotonic()` timestamp of the last `_collections`/`_collection_meta`
        # fetch (both above), or None if never fetched (or invalidated). A
        # monotonic clock, not wall-clock time, so a system clock adjustment
        # (NTP step, DST, manual change) can't make either cache appear
        # younger or older than it really is. The two caches share this one
        # timestamp rather than each tracking their own, because they are
        # always fetched together in `_ensure_collections` - giving them
        # independent timestamps would let one be treated as fresh while the
        # other is stale, which used to be possible before that method
        # existed.
        self._collections_fetched_at: float | None = None
        # True for the duration of one `get_agenda` call (see its body),
        # which needs every collection lookup inside that call to agree on
        # one server snapshot rather than possibly re-fetching partway
        # through and mixing pre- and post-refresh state. While set,
        # `_cache_expired` treats any cache that has been fetched at least
        # once as fresh regardless of its age, so nothing inside the call
        # re-fetches on its own; a cache invalidated during the call (e.g. by
        # `_with_collection`'s stale-entry retry) still has `fetched_at is
        # None` and is exempt from the freeze, so that retry still works.
        # Reset to False in `get_agenda`'s `finally`, so a raised exception
        # can't leave the freeze on for later calls.
        self._ttl_frozen: bool = False

    def _get_principal(self) -> DAVPrincipal:
        with self._lock:
            if self._principal is None:
                try:
                    self._principal = self._client.principal()
                except Exception as exc:
                    raise _translate(exc) from exc
            return self._principal

    def _get_own_organizer_address(self) -> str:
        """The caller's own "mailto:..." address, used to fill in ORGANIZER
        the first time attendees are added to an event (see
        `event_mapping.apply_event_fields`'s `own_organizer` parameter).
        """
        with self._lock:
            if self._own_organizer_address is None:
                self._own_organizer_address = self._discover_own_organizer_address()
            return self._own_organizer_address

    def _discover_own_organizer_address(self) -> str:
        """Best-effort discovery of the caller's own scheduling address.

        Tries `principal.get_vcal_address()` first (the caldav library's own
        helper for this, built on calendar-user-address-set), then falls back
        to the mailto entries of `principal.calendar_user_address_set()`
        directly. Both are RFC 6638 properties that a CalDAV server may not
        expose (or may expose empty) - if neither yields anything, this falls
        back to a `mailto:<username>` guess rather than failing outright,
        since the caller's own username is at least a plausible address on
        most Nextcloud instances (username == email is common), and an event
        can still be created without a perfect ORGANIZER.
        """
        principal = self._get_principal()
        try:
            address = str(principal.get_vcal_address()).strip()
            if address:
                return address
        except Exception:
            logger.debug("principal.get_vcal_address() unavailable", exc_info=True)
        try:
            for addr in principal.calendar_user_address_set() or []:
                if addr and str(addr).strip().lower().startswith("mailto:"):
                    return str(addr).strip()
        except Exception:
            logger.debug("principal.calendar_user_address_set() unavailable", exc_info=True)
        return f"mailto:{self._username}"

    def _get_own_calendar_user_addresses(self) -> list[str]:
        """Every CalDAV calendar-user-address of the caller (RFC 6638), used by
        `respond_to_event` to find "my" ATTENDEE entry on an event.
        """
        with self._lock:
            if self._own_calendar_user_addresses is None:
                self._own_calendar_user_addresses = self._discover_own_calendar_user_addresses()
            return self._own_calendar_user_addresses

    def _discover_own_calendar_user_addresses(self) -> list[str]:
        principal = self._get_principal()
        try:
            addresses = [str(a).strip() for a in (principal.calendar_user_address_set() or []) if a]
            if addresses:
                return addresses
        except Exception:
            logger.debug("principal.calendar_user_address_set() unavailable", exc_info=True)
        # No usable address set from the server - the single best-effort
        # organizer address (which has its own mailto:<username> fallback)
        # is at least something to compare ATTENDEEs against.
        return [self._get_own_organizer_address()]

    def _home_set_url(self) -> Any:
        """The calendar-home-set collection URL (`.../calendars/<user>/`).

        Derived from the DAV root the same way the trashbin URLs are - a
        Nextcloud path assumption this whole module already relies on - so
        learning it costs no extra principal/home-set discovery round-trip.
        """
        return self._client.url.join(f"calendars/{self._username}/")

    def _cache_expired(self, fetched_at: float | None) -> bool:
        """True if a `_collections`/`_collection_meta` cache last (re)fetched at
        `fetched_at` (a `monotonic()` reading) is past
        `_COLLECTION_CACHE_TTL_SECONDS`, or was never fetched at all.

        While `_ttl_frozen` is set (during a `get_agenda` call, see its body),
        this returns `False` for any cache that has been fetched at least
        once (`fetched_at is not None`), no matter how old - deliberately: it
        is what keeps `get_agenda` from re-fetching partway through and
        mixing pre- and post-refresh collection state into one answer. A
        cache that has *never* been fetched (`fetched_at is None`, e.g. one
        just invalidated by a stale-entry retry) is never treated as fresh,
        freeze or not, so recovery from a genuinely stale entry still works
        during a frozen call.
        """
        if self._ttl_frozen and fetched_at is not None:
            return False
        return fetched_at is None or monotonic() - fetched_at >= _COLLECTION_CACHE_TTL_SECONDS

    def _ensure_collections(self, *, fresh: bool = False) -> None:
        """Refresh `_collections` and `_collection_meta` together if needed.

        Both are fetched and assigned in one pass - collections first, then
        the batched metadata PROPFIND, then both caches and
        `_collections_fetched_at` are all set together - so no caller can
        ever observe one refreshed and the other still on the previous
        fetch, and a failure partway through (either network call raising)
        leaves both previous caches untouched rather than swapping in a
        half-updated pair. `fresh=True` forces a refetch regardless of the
        TTL (used by the create/rename/update conflict checks). Call under
        `_lock`.
        """
        if (
            fresh
            or self._collections is None
            or self._collection_meta is None
            or self._cache_expired(self._collections_fetched_at)
        ):
            try:
                collections = list(self._get_principal().calendars())
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            meta = self._fetch_collection_meta()

            self._collections = collections
            self._collection_meta = meta
            self._collections_fetched_at = monotonic()

    def _get_collection_meta(self) -> dict[str, dict[str, Any]]:
        """The cached per-collection metadata map."""
        with self._lock:
            self._ensure_collections()
            assert self._collection_meta is not None
            return self._collection_meta

    def _fetch_collection_meta(self) -> dict[str, dict[str, Any]]:
        """One Depth-1 PROPFIND over the calendar-home-set, parsed into
        `{normalized href: {"components": set, "color": str|None}}`.

        Best-effort: any failure (non-Nextcloud layout, transient error,
        unparseable response) yields an empty map rather than raising, so
        callers fall back to the per-calendar lookup and correctness is
        preserved - only the batching speed-up is lost for that call.
        """
        meta: dict[str, dict[str, Any]] = {}
        try:
            response = self._client.request(
                str(self._home_set_url()),
                "PROPFIND",
                _collection_props_propfind_body(),
                {"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
            )
            for href, props in _iter_multistatus_responses(response.tree):
                color_el = props.get(_clark(_ICAL_NS, "calendar-color"))
                meta[_normalize_collection_href(href)] = {
                    "components": _parse_supported_components(
                        props.get(_clark(_CALDAV_NS, "supported-calendar-component-set"))
                    ),
                    "color": (
                        color_el.text.strip() if color_el is not None and color_el.text else None
                    ),
                }
        except Exception:
            logger.debug("Batched collection-metadata PROPFIND failed", exc_info=True)
            return {}
        return meta

    def _list_collections(self, *, fresh: bool = False) -> list[DAVCalendar]:
        """The account's collections (`principal.calendars()`), cached for up
        to `_COLLECTION_CACHE_TTL_SECONDS`.

        Every caller used to invoke `principal.calendars()` directly, which
        caldav answers with two PROPFINDs (calendar-home-set discovery + the
        Depth-1 listing) that it never caches - so every listing and every
        cold name resolution repaid them. The resolved list is reused across
        calls except when a collection is created/deleted/renamed here (all
        of which invalidate the cache), a cached collection turns out stale
        mid-request (`_with_collection` invalidates, so the retry's
        resolution re-fetches), or the cache has simply gone past its TTL -
        the last case is what catches a collection renamed/deleted through
        the Nextcloud web UI (or any other client), which none of this
        process's own invalidation hooks can see. `fresh=True` forces a
        re-fetch regardless of the TTL, for the create/rename/update conflict
        checks, which must not decide "available" from a stale list. Guarded
        by `_lock`.
        """
        with self._lock:
            self._ensure_collections(fresh=fresh)
            assert self._collections is not None
            return self._collections

    def _invalidate_collection_caches(self) -> None:
        """Drop the cached collection list and metadata after the collection
        set (or a color) changes, so the next lookup re-fetches. Call under
        `_lock`."""
        self._collections = None
        self._collections_fetched_at = None
        self._collection_meta = None

    def _collection_meta_for(self, calendar: DAVCalendar) -> dict[str, Any] | None:
        return self._get_collection_meta().get(_normalize_collection_href(str(calendar.url)))

    def _supported_components(self, calendar: DAVCalendar) -> set[str]:
        """`calendar`'s advertised component set, from the batched metadata.

        Falls back to caldav's per-calendar `get_supported_components()` only
        when the calendar isn't in the batch (e.g. the home-set PROPFIND
        failed, or a subscription collection lives outside it), so the fast
        path costs no request while unusual layouts still resolve correctly.
        An empty set (nothing advertised, or the fallback failed) means
        "supports everything" to callers.
        """
        meta = self._collection_meta_for(calendar)
        if meta is not None:
            return meta["components"]
        try:
            return set(calendar.get_supported_components())
        except Exception:
            return set()

    def _supports_component(self, calendar: DAVCalendar, component: str) -> bool:
        """True if `calendar` supports `component` ("VTODO"/"VEVENT"), or can't tell.

        Nextcloud advertises `supported-calendar-component-set` on every
        calendar, but a collection that doesn't (or whose lookup fails, e.g.
        an external webcal subscription with flaky props) is treated as
        supporting everything - failing open here only means a name shows up
        in one listing too many, while failing closed would make an entire
        calendar silently unreachable. The component set is read from the
        batched, cached metadata (see `_supported_components`) rather than a
        per-calendar PROPFIND.
        """
        components = self._supported_components(calendar)
        return not components or component in components

    @staticmethod
    def _kind_label(component: str) -> str:
        return "calendar" if component == "VEVENT" else "task list"

    def _not_found(self, name: str, component: str) -> TaskMcpError:
        if component == "VEVENT":
            return CalendarNotFoundError(f"Calendar '{name}' was not found.")
        return TaskListNotFoundError(f"Task list '{name}' was not found.")

    def _resolve_collection(self, name: str, component: str) -> DAVCalendar:
        """Resolve a display name to a collection supporting `component`, freshly.

        Nextcloud keeps task lists (VTODO) and event calendars (VEVENT) side
        by side under `/calendars/<user>/`, so resolution filters by
        component support - asking for the task list "Personal" must not
        return an events-only calendar of the same name. Raises the
        kind-specific not-found error if nothing matches, or a generic
        `TaskMcpError` if more than one does - a duplicate display name is
        genuinely ambiguous, so callers are told to rename rather than have
        the server silently pick one (A3).
        """
        calendars = self._list_collections()

        matches = [
            c
            for c in calendars
            if c.get_display_name() == name and self._supports_component(c, component)
        ]
        if not matches:
            raise self._not_found(name, component)
        if len(matches) > 1:
            kind = self._kind_label(component)
            raise TaskMcpError(
                f"Multiple {kind}s are named '{name}', which is ambiguous. "
                f"Rename the {kind}s in Nextcloud so each has a distinct name, or "
                "use a different, unambiguous name."
            )
        return matches[0]

    def _cache_collection(
        self,
        component: str,
        name: str,
        calendar: DAVCalendar,
        *,
        fetched_at: float | None = None,
    ) -> None:
        """Remember `name`'s resolved collection."""
        if fetched_at is None:
            fetched_at = self._collections_fetched_at or monotonic()
        self._calendar_cache[(component, name)] = (calendar, fetched_at)

    def _resolve_target_collection(self, name: str, component: str) -> DAVCalendar:
        """Resolve target collection by name, validating component support.

        Raises kind-specific not-found error if no collection with `name` exists,
        or a speaking error naming both target and component kind if it exists but
        does not support `component`.
        """
        calendars = self._list_collections()
        same_name = [c for c in calendars if c.get_display_name() == name]
        if not same_name:
            raise self._not_found(name, component)

        matches = [c for c in same_name if self._supports_component(c, component)]
        if not matches:
            kind_plural = "events" if component == "VEVENT" else "tasks"
            kind_label = "Calendar" if component == "VEVENT" else "Task list"
            raise TaskMcpError(f"{kind_label} '{name}' does not accept {kind_plural}.")

        if len(matches) > 1:
            kind = self._kind_label(component)
            raise TaskMcpError(
                f"Multiple {kind}s are named '{name}', which is ambiguous. "
                f"Rename the {kind}s in Nextcloud so each has a distinct name, or "
                "use a different, unambiguous name."
            )
        calendar = matches[0]
        self._cache_collection(component, name, calendar)
        return calendar

    def _resolve_and_cache(self, name: str, component: str) -> DAVCalendar:
        calendar = self._resolve_collection(name, component)
        # `fetched_at` defaults to `self._collections_fetched_at` already -
        # no need to pass it explicitly here.
        self._cache_collection(component, name, calendar)
        return calendar

    def _get_collection(self, name: str, component: str) -> DAVCalendar:
        cached = self._calendar_cache.get((component, name))
        if cached is not None:
            calendar, cached_at = cached
            if not self._cache_expired(cached_at):
                return calendar
            # Past the TTL, re-resolve rather than trust the entry: the name
            # may since have been given to a different collection (see the
            # cache's declaration).
            del self._calendar_cache[(component, name)]
        self._ensure_collections()
        return self._resolve_and_cache(name, component)

    def _with_collection(self, name: str, component: str, fn: Callable[[DAVCalendar], _T]) -> _T:
        """Resolve `name`'s (cached) collection and call `fn(calendar)`.

        `fn` should perform raw caldav operations without translating
        `caldav_error.NotFoundError` itself: a cached calendar may have gone
        stale (the collection was deleted/renamed server-side since it was
        cached), which surfaces as that same NotFoundError on the actual
        request. On that specific error, the stale cache entry is dropped
        and resolution is retried exactly once with a fresh
        `principal.calendars()` call before giving up (A3) - this keeps the
        common case cheap while still recovering from a stale cache instead
        of failing (or silently misbehaving) forever.
        """
        calendar = self._get_collection(name, component)
        try:
            return fn(calendar)
        except caldav_error.NotFoundError:
            self._calendar_cache.pop((component, name), None)
            # The collection list itself may be stale (the collection was
            # deleted/renamed server-side), so drop it too - re-resolution
            # must go back to the server for a fresh listing, not reuse the
            # cached one that still lists the vanished collection.
            self._invalidate_collection_caches()
            calendar = self._resolve_and_cache(name, component)
            return fn(calendar)

    def _with_calendar(self, list_name: str, fn: Callable[[DAVCalendar], _T]) -> _T:
        """Task-list flavour of `_with_collection`, kept for the VTODO call sites."""
        return self._with_collection(list_name, "VTODO", fn)

    def _resolve_collection_any(self, name: str) -> DAVCalendar:
        """Resolve `name` to a collection of either kind (task list or event calendar).

        Used by the cross-cutting Nextcloud features below (sharing, ICS
        export/import) that operate identically on both kinds. Tries the
        VEVENT-supporting collections first, then VTODO; an ambiguous name
        found within either kind is raised immediately - matching
        `_resolve_collection`'s "don't guess" stance - rather than silently
        falling through to try the other kind.
        """
        try:
            return self._resolve_collection(name, "VEVENT")
        except (CalendarNotFoundError, TaskListNotFoundError):
            pass
        try:
            return self._resolve_collection(name, "VTODO")
        except (CalendarNotFoundError, TaskListNotFoundError) as exc:
            raise TaskMcpError(f"Calendar or task list '{name}' was not found.") from exc

    def _dav_request(
        self,
        url: Any,
        method: str,
        body: str,
        headers: dict[str, str],
        *,
        forbidden_message: str,
    ) -> Any:
        """Send a raw DAV request via the shared session, translating errors.

        Nextcloud's sharing/trashbin extensions used below are outside plain
        CalDAV and have no caldav-library API, so these go straight through
        `DAVClient.request`. Its own auth-negotiation logic already turns any
        HTTP 401/403 response into `caldav_error.AuthorizationError` before
        this ever sees a response object (see `DAVClient._sync_request`) -
        `_translate` would turn that into a generic "bad credentials"
        message, which is misleading for a 403 that actually means "no
        permission for this specific operation", so it's caught here and
        replaced with the caller-supplied, operation-specific
        `forbidden_message` instead. Any other response (including 404 and
        other 4xx/5xx) comes back as a normal `DAVResponse` for the caller to
        inspect via `.status`.
        """
        try:
            return self._client.request(str(url), method, body, headers)
        except caldav_error.AuthorizationError as exc:
            raise TaskMcpError(forbidden_message) from exc
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

    def _trashbin_objects_url(self) -> Any:
        return self._client.url.join(f"calendars/{self._username}/trashbin/objects/")

    def _trashbin_restore_url(self) -> Any:
        return self._client.url.join(f"calendars/{self._username}/trashbin/restore/")

    def list_task_lists(self) -> list[dict[str, str]]:
        """Return all VTODO-supporting calendars on the account as {"name", "url"} dicts.

        Event-only calendars (VEVENT, e.g. Nextcloud's default "Personal"
        calendar) are excluded - they live in the same DAV namespace but
        can't hold tasks, so listing them here would invite task operations
        that the server then rejects. `list_calendars` is the event-side
        counterpart.
        """
        with self._lock:
            calendars = self._list_collections()

            calendars = [c for c in calendars if self._supports_component(c, "VTODO")]
            names = [calendar.get_display_name() or str(calendar.url) for calendar in calendars]
            name_counts: dict[str, int] = {}
            for name in names:
                name_counts[name] = name_counts.get(name, 0) + 1
            # Populate the resolution cache opportunistically (A3), but only
            # for names that are actually unambiguous - caching one of
            # several same-named calendars would silently hide the
            # ambiguity that `_resolve_collection` is supposed to surface.
            for calendar, name in zip(calendars, names, strict=True):
                if name_counts[name] == 1:
                    self._cache_collection(
                        "VTODO", name, calendar, fetched_at=self._collections_fetched_at
                    )

            return [
                {"name": name, "url": str(calendar.url)}
                for calendar, name in zip(calendars, names, strict=True)
            ]

    def create_task_list(self, display_name: str) -> dict[str, str]:
        """Create a new Nextcloud task list (a CalDAV calendar collection supporting VTODO).

        The collection id (the last path segment of its URL) is derived from
        `display_name` via `_slugify` rather than left to caldav's own
        default (a random UUID) - a human still has to look at this URL in
        Nextcloud's web UI or a CalDAV client, so a readable id is worth
        generating deliberately.

        A display-name conflict (another list already has this exact name,
        checked proactively via `principal.calendars()` before the
        server-side create) is rejected rather than silently handled,
        mirroring `_resolve_collection`'s "don't guess" stance on ambiguous
        names. A collision of the generated collection *id*, by contrast, is
        dodged automatically - see `_make_collection`.

        Returns:
            {"name": display name, "url": internal CalDAV URL} for the new
            list, matching one entry of `list_task_lists`'s return value.
        """
        if not display_name or not display_name.strip():
            raise InvalidTaskDataError("display_name is required to create a task list.")

        slug = _slugify(display_name)

        with self._lock:
            # Fresh list for the conflict check - a stale cache must not let a
            # name that already exists slip through as "available".
            existing = self._list_collections(fresh=True)
            principal = self._get_principal()

            if any(
                calendar.get_display_name() == display_name
                and self._supports_component(calendar, "VTODO")
                for calendar in existing
            ):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{display_name}' already exists."
                )

            calendar = self._make_collection(
                principal,
                display_name,
                slug,
                component="VTODO",
                conflict_error=TaskListAlreadyExistsError,
                kind="task list",
            )

            self._invalidate_collection_caches()
            self._cache_collection("VTODO", display_name, calendar, fetched_at=monotonic())
            return {"name": display_name, "url": str(calendar.url)}

    def _make_collection(
        self,
        principal: DAVPrincipal,
        display_name: str,
        slug: str,
        *,
        component: str,
        conflict_error: type[TaskMcpError],
        kind: str,
    ) -> DAVCalendar:
        """MKCALENDAR a new collection, dodging occupied collection ids.

        A 405 (Method Not Allowed) / 409 (Conflict) response means the
        collection URL is already taken - either by a different-named
        collection whose name slugifies to the same id, or by a *deleted*
        collection still sitting in Nextcloud's trashbin, which keeps its URI
        occupied (invisibly - it no longer shows up in listings) until the
        trash is purged. Since the display name is this API's identity and
        the id is internal, the id is not worth failing over: retry with
        "<slug>-2", "<slug>-3", ... before giving up. Display-name conflicts
        are still rejected by the callers, before ever getting here.
        """
        candidates = [slug] + [f"{slug}-{i}" for i in range(2, 7)]
        for cal_id in candidates:
            try:
                return principal.make_calendar(
                    name=display_name,
                    cal_id=cal_id,
                    supported_calendar_component_set=[component],
                )
            except (caldav_error.MkcolError, caldav_error.MkcalendarError) as exc:
                if "405" in str(exc) or "409" in str(exc):
                    continue  # id occupied - try the next candidate
                logger.warning("CalDAV request failed creating %s", kind, exc_info=exc)
                raise TaskMcpError("The CalDAV request failed on the Nextcloud server.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
        raise conflict_error(
            f"Could not create the {kind} '{display_name}': every generated collection id "
            f"('{candidates[0]}' through '{candidates[-1]}') is already taken on the server "
            "(possibly by deleted collections still in the trashbin). "
            "Try a different display name."
        )

    def delete_task_list(self, list_name: str) -> None:
        """Permanently delete a Nextcloud task list and every task inside it.

        This is irreversible from this API's point of view: deleting the
        underlying CalDAV calendar collection deletes all VTODOs it contains
        along with it (the server may retain them in a trashbin, but this
        client has no way to recover them). Callers should confirm with the
        user before calling this.

        Resolution goes through the same (cached) `_with_calendar` path as
        `delete_task`/`update_task`/etc., so a `list_name` that isn't
        currently cached costs one `principal.calendars()` PROPFIND, and a
        stale cache entry (list already deleted/recreated server-side) is
        retried once against a fresh resolution before giving up (A3).
        """

        def op(calendar: DAVCalendar) -> None:
            calendar.delete()

        with self._lock:
            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            # The list is gone - drop it from the cache so a later call
            # with this name resolves fresh instead of reusing a deleted
            # calendar's (now-invalid) object.
            self._calendar_cache.pop(("VTODO", list_name), None)
            self._invalidate_collection_caches()

    def rename_task_list(self, list_name: str, new_display_name: str) -> dict[str, str]:
        """Rename a Nextcloud task list (change its CalDAV displayname property).

        Only the display name changes - the collection's URL/id is left
        alone, so any client that referenced the list by URL is unaffected.

        Mirrors `create_task_list`'s "don't guess" stance on name conflicts:
        `principal.calendars()` is fetched fresh (not from the cache) so both
        resolving `list_name` and checking `new_display_name` for conflicts
        see the current server state in one round-trip. Renaming a list to
        the name it already has is a no-op success rather than a
        self-conflict; renaming it to a name some *other* list already has
        raises `TaskListAlreadyExistsError` instead of silently producing two
        identically-named lists (which `_resolve_calendar` would then report
        as ambiguous).

        Returns:
            {"name": new display name, "url": internal CalDAV URL} for the
            renamed list, matching one entry of `list_task_lists`'s return
            value.
        """
        if not new_display_name or not new_display_name.strip():
            raise InvalidTaskDataError("new_display_name is required to rename a task list.")

        with self._lock:
            existing = self._list_collections(fresh=True)

            matches = [
                c
                for c in existing
                if c.get_display_name() == list_name and self._supports_component(c, "VTODO")
            ]
            if not matches:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.")
            if len(matches) > 1:
                raise TaskMcpError(
                    f"Multiple task lists are named '{list_name}', which is ambiguous. "
                    "Rename the task lists in Nextcloud so each has a distinct name, or "
                    "use a different, unambiguous list name."
                )
            calendar = matches[0]

            if new_display_name != list_name and any(
                c.get_display_name() == new_display_name and self._supports_component(c, "VTODO")
                for c in existing
            ):
                raise TaskListAlreadyExistsError(
                    f"A task list named '{new_display_name}' already exists."
                )

            try:
                calendar.set_properties([dav.DisplayName(new_display_name)])
            except Exception as exc:
                raise _translate(exc) from exc

            self._calendar_cache.pop(("VTODO", list_name), None)
            # The cached collection list holds this object with its now-stale
            # display name, so drop it (component/color metadata is keyed by
            # href and unaffected, but is cleared together for simplicity).
            self._invalidate_collection_caches()
            self._cache_collection("VTODO", new_display_name, calendar, fetched_at=monotonic())
            return {"name": new_display_name, "url": str(calendar.url)}

    def list_tasks(
        self,
        list_names: list[str] | str | None = None,
        only_open: bool = True,
        due_before: str | None = None,
        due_after: str | None = None,
        limit: int | None = None,
        *,
        prioritaet: str | None = None,
        tag: str | None = None,
        suchtext: str | None = None,
        ohne_erinnerung: bool = False,
        ohne_sichtbarkeit: bool = False,
        ohne_tags: bool = False,
        uid_regex: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return tasks across one, several, or all VTODO task lists, parsed into German task dicts.

        `list_names` is a display name, a list of them, or `None` for every
        task list on the account; an empty list is an empty scope (no request,
        no results - the filter arguments are still validated). Repeating a
        name queries that list once, not twice. `""` is a name like any other,
        i.e. an unknown one.

        `due_before`/`due_after`/`prioritaet`/`tag`/`suchtext`/`limit` - and the
        cleanup filters `ohne_erinnerung`/`ohne_sichtbarkeit`/`ohne_tags`/
        `uid_regex` - filter the
        parsed results via `mapping.filter_tasks`, which also expands recurring
        tasks into their occurrences when `due_before` bounds the window (see
        `mapping._expand_recurring_tasks`). Each task dict gains a "liste"
        key set to its task list display name. That name is what every other task
        tool takes, with one honest exception: Nextcloud permits two task lists to
        share a display name, and then "liste" cannot tell them apart (nor can any
        by-name call - `_resolve_collection` reports such a name as ambiguous
        rather than guessing). Renaming one of them in Nextcloud is the only fix.

        Anything added to this signature goes after `limit`, keyword-only: the
        parameters up to and including `limit` are a positional prefix callers
        may rely on.
        """
        if isinstance(list_names, str):
            list_names = [list_names]
        if list_names is not None and not list_names:
            # Nothing to query, but a caller passing limit=0 or an unknown
            # prioritaet still deserves to hear about it rather than get a
            # plausible-looking empty result.
            return mapping.filter_tasks(
                [],
                due_before=due_before,
                due_after=due_after,
                prioritaet=prioritaet,
                tag=tag,
                suchtext=suchtext,
                limit=limit,
                ohne_erinnerung=ohne_erinnerung,
                ohne_sichtbarkeit=ohne_sichtbarkeit,
                ohne_tags=ohne_tags,
                uid_regex=uid_regex,
            )

        with self._lock:
            if list_names is None:
                tasks = self._tasks_from_every_list(only_open)
            else:
                tasks = self._tasks_from_named_lists(list_names, only_open)

        return mapping.filter_tasks(
            tasks,
            due_before=due_before,
            due_after=due_after,
            prioritaet=prioritaet,
            tag=tag,
            suchtext=suchtext,
            limit=limit,
            ohne_erinnerung=ohne_erinnerung,
            ohne_sichtbarkeit=ohne_sichtbarkeit,
            ohne_tags=ohne_tags,
            uid_regex=uid_regex,
        )

    def _parse_todos(
        self, todos: Iterable[Any], list_name: str, list_url: str
    ) -> list[dict[str, Any]]:
        """Parse one list's VTODO objects, stamping each with the list it came from."""
        parsed = []
        for todo in todos:
            task = mapping.parse_vtodo(todo.icalendar_component)
            task["liste"] = list_name
            task["liste_url"] = list_url
            parsed.append(task)
        return parsed

    def _tasks_from_named_lists(
        self, list_names: list[str], only_open: bool
    ) -> list[dict[str, Any]]:
        """Tasks from the named lists, each queried through the cache-aware path.

        Names are de-duplicated (keeping the caller's order): the same list
        named twice used to be fetched twice, so every one of its tasks
        appeared twice in the result. Each name is resolved once, inside
        `_with_collection` - which also means a resolution failure is
        translated like any other CalDAV error, and a stale cache entry is
        re-resolved once.
        """

        def op(calendar: DAVCalendar):
            return calendar.todos(include_completed=not only_open), str(calendar.url)

        tasks: list[dict[str, Any]] = []
        for name in dict.fromkeys(list_names):
            try:
                todos, url = self._with_collection(name, "VTODO", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            tasks.extend(self._parse_todos(todos, name, url))
        return tasks

    def _tasks_from_every_list(
        self, only_open: bool, *, may_retry: bool = True
    ) -> list[dict[str, Any]]:
        """Tasks from every VTODO collection on the account.

        The collections are queried as the objects the (cached) listing
        returned rather than re-resolved by name, which is what keeps two
        lists sharing a display name both reachable - that name is ambiguous
        on purpose. The cost is that `_with_collection`'s stale-cache retry
        doesn't apply here, so it is done once over the whole pass instead: a
        404 anywhere in the pass means the cached listing is out of date (the
        list was deleted or recreated server-side), the caches are dropped and
        the pass is repeated against a freshly listed set. Without it, one
        deleted task list would make every all-lists query fail for the
        remaining life of the process.

        "Anywhere in the pass" includes enumerating the lists: reading a
        cached collection's display name is itself a request, so `_task_lists`
        is inside the retry rather than in front of it. A stale object found
        there would otherwise escape as a generic not-found error with the
        cache left untouched - the same permanent failure, one call earlier.
        """
        tasks: list[dict[str, Any]] = []
        name: str | None = None
        try:
            try:
                for name, calendar in self._task_lists():
                    todos = calendar.todos(include_completed=not only_open)
                    tasks.extend(self._parse_todos(todos, name, str(calendar.url)))
            except caldav_error.NotFoundError as exc:
                if may_retry:
                    self._invalidate_collection_caches()
                    return self._tasks_from_every_list(only_open, may_retry=False)
                # A freshly listed collection that still 404s is genuinely
                # gone mid-request, not a stale cache entry. `name` is None
                # when the enumeration itself failed, before any list was
                # named.
                raise TaskListNotFoundError(
                    f"Task list '{name}' was not found."
                    if name is not None
                    else "A task list was not found while listing the task lists."
                ) from exc
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc
        return tasks

    def create_task(self, list_name: str, fields: mapping.TaskFields) -> str:
        """Create a new task in the given list and return its UID."""
        if fields.titel is None:
            raise InvalidTaskDataError("titel is required to create a task.")
        with self._lock:
            new_uid = str(uuid.uuid4())
            todo = Todo()
            todo.add("uid", new_uid)
            todo.add("dtstamp", datetime.now(timezone.utc))
            mapping.apply_task_fields(todo, fields)

            vcal = Calendar()
            vcal.add("prodid", "-//nextcloud-task-mcp//EN")
            vcal.add("version", "2.0")
            _sync_vtimezones(vcal, todo)
            vcal.add_component(todo)
            ical_text = vcal.to_ical().decode("utf-8")

            def op(calendar: DAVCalendar):
                calendar.save_todo(ical=ical_text)

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return new_uid

    def update_task(self, list_name: str, task_uid: str, fields: mapping.TaskFields) -> None:
        """Update only the given (non-None) fields of an existing task."""
        _reject_occurrence_uid(task_uid)
        with self._lock:

            def op(calendar: DAVCalendar):
                _apply_task_patch(calendar.get_todo_by_uid(task_uid), fields)

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def get_task(self, list_name: str, task_uid: str) -> dict[str, Any]:
        """Return a single task, parsed into the server's German task dict."""
        _reject_occurrence_uid(task_uid)
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.get_todo_by_uid(task_uid)

            try:
                todo_obj = self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return mapping.parse_vtodo(todo_obj.icalendar_component)

    def complete_task(self, list_name: str, task_uid: str) -> None:
        """Mark a task as completed (STATUS, PERCENT-COMPLETE, COMPLETED timestamp)."""
        _reject_occurrence_uid(task_uid)
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                mapping.mark_completed(todo_obj.icalendar_component)
                todo_obj.save()

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def delete_task(self, list_name: str, task_uid: str) -> None:
        """Permanently delete a task."""
        _reject_occurrence_uid(task_uid)
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                todo_obj.delete()

            try:
                self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def move_task(
        self,
        list_name: str,
        task_uid: str,
        target_list: str,
        uebergeordnete_aufgabe: str | None = None,
        clear: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        """Move a task from one task list to another, optionally re-parenting it.

        Prefers a CalDAV MOVE request (preserving server-side URL identity and
        ETags). Falls back to copy-then-delete if the server rejects MOVE with
        403/405/409/501, and retries it instead if the server gives no answer
        at all (502/503/504).

        Args:
            list_name: Display name of the source task list.
            task_uid: UID of the task to move.
            target_list: Display name of the target task list.
            uebergeordnete_aufgabe: Optional new parent task UID, applied in the
                *target* list once the move succeeded. As in `update_task`, this
                replaces the task's RELATED-TO rather than adding to it.
            clear: Optional `("uebergeordnete_aufgabe",)` to detach the task from
                its parent instead - the usual counterpart of a move, since a
                parent left behind in the source list would otherwise keep the
                moved task nested under a task in another list. Any other field
                name raises `InvalidTaskDataError`; use `update_task` for those.

        Returns:
            {"uid": task_uid, "von": source, "nach": target,
            "methode": "MOVE" | "kopiert" | "bereits_dort"}, plus
            "hierarchie": "gesetzt" | "geleert" when the parent was changed,
            plus "verwaiste_verknuepfungen" (see `_orphaned_subtask_links`).
        """
        clear_parent = _validate_move_clear(
            clear,
            _MOVE_TASK_CLEARABLE,
            uebergeordnete_aufgabe,
            InvalidTaskDataError,
        )
        # Checked before the move so a bad UID can't leave the task moved but
        # un-reparented; `update_task` would reject it at the same point.
        _reject_occurrence_uid(task_uid)

        with self._lock:
            result: dict[str, Any] = dict(
                self._move_object(list_name, task_uid, target_list, "VTODO")
            )
            if uebergeordnete_aufgabe is not None or clear_parent:
                fields = mapping.TaskFields(
                    uebergeordnete_aufgabe=uebergeordnete_aufgabe,
                    clear=(_MOVE_TASK_CLEARABLE,) if clear_parent else (),
                )
                self._apply_move_hierarchy(
                    lambda: self.update_task(target_list, task_uid, fields),
                    uid=task_uid,
                    target_display=result["nach"],
                    kind_article="Task",
                    field_name=_MOVE_TASK_CLEARABLE,
                    retry_tool="update_task",
                )
                result["hierarchie"] = "geleert" if clear_parent else "gesetzt"

            # Deliberately last: a caller that re-parented in this same call
            # has already repaired the link the move would otherwise have
            # orphaned, and the scan re-reads both lists, so it reports the
            # state the call actually leaves behind rather than the one the
            # bare move would have.
            result["verwaiste_verknuepfungen"] = self._orphaned_subtask_links(
                list_name, target_list, task_uid
            )
            return result

    def _apply_move_hierarchy(
        self,
        write: Callable[[], None],
        *,
        uid: str,
        target_display: str,
        kind_article: str,
        field_name: str,
        retry_tool: str,
    ) -> None:
        """Run a move's follow-up hierarchy write, reporting a partial failure honestly.

        The hierarchy change is deliberately applied *after* the move, in the
        target collection: the move is the primary operation, so a failure here
        leaves a consistent object in the right collection with a stale parent
        link - rather than the reverse order's failure mode, where a rejected
        move would strand the object in the source collection pointing at a
        parent that lives in the target one. The caller is told both halves so
        it can retry just the part that did not happen.
        """
        try:
            write()
        except TaskMcpError as exc:
            raise TaskMcpError(
                f"{kind_article} '{uid}' was moved to '{target_display}', but changing its "
                f"{field_name} there failed: {exc} The move itself stands - retry only the "
                f"hierarchy change with {retry_tool} on '{target_display}'."
            ) from exc

    def _subtask_links(self, calendar: DAVCalendar) -> dict[str, _TaskLink]:
        """{UID: (parent UID or None, title)} for every task in a collection.

        Only the three properties the hierarchy is made of are read - not a
        full `parse_vtodo` - because this runs over two entire task lists to
        answer one warning, and because a VTODO another client wrote oddly
        must not turn that warning into a failed move. Completed tasks are
        included: a done subtask is still nested under its parent. A task
        whose UID cannot be read is skipped - nothing can point at it either.
        """
        links: dict[str, _TaskLink] = {}
        for todo in calendar.todos(include_completed=True):
            try:
                component = todo.icalendar_component
                uid = component.get("uid")
                if uid is None:
                    continue
                links[str(uid)] = _TaskLink(
                    parent=mapping.extract_parent_uid(component),
                    titel=str(component.get("summary", "")),
                )
            except Exception:  # noqa: BLE001 - one odd task must not break the scan
                continue
        return links

    def _orphaned_subtask_links(
        self, source_name: str, target_name: str, task_uid: str
    ) -> list[dict[str, str]] | None:
        """Subtask links left pointing at a UID that is no longer in the same list.

        Nextcloud Tasks resolves `RELATED-TO;RELTYPE=PARENT` within one task
        list, so moving one half of a parent/child pair across lists breaks the
        hierarchy without breaking anything the move itself can see: the moved
        task keeps a RELATED-TO its new list has no parent for, and any subtask
        left behind keeps one pointing at the parent that just left. Neither is
        an error anywhere, and neither list shows the task as nested any more -
        so the move reports them instead of leaving them to be discovered.

        `move_task`'s own `uebergeordnete_aufgabe`/`clear` runs before this
        scan, so what is reported is the state the *call* leaves behind, not
        the one the bare move would have: a caller that re-parented is not
        warned about the link it just repaired, and one that re-pointed a task
        at a parent in some third list is warned about the link it just
        created. The subtasks left behind are the half no argument to a move
        can reach - each is a separate object in the source list - so those are
        reported either way.

        Returns one entry per dangling link, from the perspective of the task
        that carries it (RELATED-TO always sits on the *child*):
        `{"uid", "titel", "liste", "fehlende_uebergeordnete_uid"}`. An empty
        list means the call left the hierarchy intact.

        Returns `None` - not `[]` - when the check itself could not be run.
        The move has already succeeded by then, so its failure must not fail
        the call; but "could not tell" must not read as "all clear" either.
        """
        try:
            with self._lock:
                source_col = self._resolve_collection(source_name, "VTODO")
                target_col = self._resolve_collection(target_name, "VTODO")
                # A same-list call moves nothing, so it leaves no subtask
                # behind - but it can still have re-pointed the task itself at
                # a parent in another list, which is why it is not simply
                # answered with an empty list. One read serves both sides.
                same_list = _normalize_collection_href(str(source_col.url)) == (
                    _normalize_collection_href(str(target_col.url))
                )
                source_display = source_col.get_display_name() or source_name
                target_display = target_col.get_display_name() or target_name
                source_links = self._subtask_links(source_col)
                target_links = source_links if same_list else self._subtask_links(target_col)
        except Exception:  # noqa: BLE001 - a warning is never worth failing a done move
            logger.warning(
                "Could not check task '%s' for orphaned subtask links after moving it "
                "from '%s' to '%s'.",
                task_uid,
                source_name,
                target_name,
                exc_info=True,
            )
            return None

        orphans: list[dict[str, str]] = []

        # The moved task itself, now separated from a parent left behind.
        moved = target_links.get(task_uid)
        if moved is not None and moved.parent is not None and moved.parent not in target_links:
            orphans.append(
                {
                    "uid": task_uid,
                    "titel": moved.titel,
                    "liste": target_display,
                    "fehlende_uebergeordnete_uid": moved.parent,
                }
            )

        # Its subtasks, left behind in the source list still pointing at it.
        # Only when the task really left that list: on a same-list call they
        # are still sitting next to their parent, perfectly nested.
        if not same_list:
            for uid, link in source_links.items():
                if link.parent == task_uid:
                    orphans.append(
                        {
                            "uid": uid,
                            "titel": link.titel,
                            "liste": source_display,
                            "fehlende_uebergeordnete_uid": task_uid,
                        }
                    )
        return orphans

    @staticmethod
    def _validate_task_patch(fields: mapping.TaskFields) -> None:
        """Reject an empty or invalid patch before a single task is written.

        The task-side twin of `_validate_event_patch`, and for the same reason:
        a batch must not stop halfway because the 40th task was the first to
        reveal a bad RRULE. The patch is applied to a throwaway VTODO first, so
        every check `apply_task_fields` performs - unknown `felder_leeren`
        names, setting and clearing the same field, bad status/visibility/
        RRULE/date, an out-of-range percentage - happens there, on nothing.

        The probe carries a DTSTART because relative reminders and `wiederholung`
        both validate against having one, and its value kind follows the patch's
        own `faellig_datum` so an all-day due date isn't judged against a timed
        probe. Whether the patch fits a *given* task stays per-task and is
        reported per UID.
        """
        patch_fields = [f.name for f in dataclasses.fields(fields) if f.name != "clear"]
        if not fields.clear and all(getattr(fields, name) is None for name in patch_fields):
            raise InvalidTaskDataError(
                "No fields to update given - set at least one field, or name one in felder_leeren."
            )

        probe = Todo()
        if fields.start_datum is None:
            # With `start_datum` in the patch, `apply_task_fields` sets DTSTART
            # on the probe itself before it validates anything against it.
            all_day_due = (
                fields.faellig_datum is not None
                and _ALL_DAY_RE.match(fields.faellig_datum) is not None
            )
            probe.add("dtstart", date(1970, 1, 1) if all_day_due else _PROBE_START)
        mapping.apply_task_fields(probe, fields)

    def update_tasks(
        self, list_name: str, task_uids: list[str], fields: mapping.TaskFields
    ) -> dict[str, Any]:
        """Apply one field patch to several tasks of a list.

        The patch is validated before the first write (`_validate_task_patch`),
        so a rejected patch leaves every task untouched instead of stopping
        halfway through the batch. Per-task outcomes follow `_batch_over_uids`.
        """
        self._validate_task_patch(fields)

        def operation(calendar: DAVCalendar, uid: str) -> None:
            # Per UID rather than up front: one occurrence UID in the list is
            # a caller mistake about that entry, not a reason to refuse the
            # other 40 tasks.
            _reject_occurrence_uid(uid)
            _apply_task_patch(calendar.get_todo_by_uid(uid), fields)

        return self._batch_over_uids(_TASK_BATCH, list_name, task_uids, operation)

    def delete_tasks(self, list_name: str, task_uids: list[str]) -> dict[str, Any]:
        """Permanently delete several tasks of a list.

        Irreversible from this API's point of view, like `delete_task`, and a
        batch multiplies the damage a wrong UID list does - callers should
        confirm with the user first. Per-task outcomes follow `_batch_over_uids`.
        """

        def operation(calendar: DAVCalendar, uid: str) -> None:
            _reject_occurrence_uid(uid)
            calendar.get_todo_by_uid(uid).delete()

        return self._batch_over_uids(_TASK_BATCH, list_name, task_uids, operation)

    def move_tasks(self, list_name: str, task_uids: list[str], target_list: str) -> dict[str, Any]:
        """Move several tasks from one list to another, reporting each outcome.

        Both lists are resolved once for the whole batch rather than twice per
        task, and each move follows `move_task` exactly - CalDAV MOVE first,
        copy-then-delete where the server refuses it, a retry where the server
        gives no answer.

        A move that fails after its retries is recorded against its UID and the
        batch carries on, because a partly-migrated list is the situation this
        exists to get out of, not one to create. Re-running the call with the
        same UIDs is safe: a task already sitting in the target is reported as
        "bereits_dort" instead of being moved again or reported as missing.
        """
        with self._lock:
            target_col = self._resolve_target_collection(target_list, "VTODO")
            target_display = target_col.get_display_name() or target_list

            def operation(calendar: DAVCalendar, uid: str) -> dict[str, Any]:
                _reject_occurrence_uid(uid)
                # `_move_one` already raises `ObjectMoveError` for every
                # halfway point that concerns one task (a UID the target
                # already holds, a copy the server would not accept, a source
                # it would not delete), which `_batch_over_uids` records per
                # UID. Whatever else reaches here is call-scoped - rate
                # limiting, a forbidden request, a gateway that kept not
                # answering - and must be left to abort the batch. Re-badging
                # it as an `ObjectMoveError` would instead have the batch put
                # the same broken request to the server another 199 times.
                return self._move_one(
                    calendar,
                    target_col,
                    uid,
                    "VTODO",
                    calendar.get_display_name() or list_name,
                    target_display,
                )

            return self._batch_over_uids(_TASK_BATCH, list_name, task_uids, operation)

    # ------------------------------------------------------------------
    # Event calendars (VEVENT)
    # ------------------------------------------------------------------

    @staticmethod
    def _range_bound(value: str | None, *, exclusive_end: bool) -> datetime | None:
        """Normalize a `von`/`bis` filter value to a timezone-aware datetime.

        A date-only value expands to the start of that day (for `von`) or the
        start of the *next* day (for `bis`, making a date-only upper bound
        inclusive of the whole day - the resulting datetime is used as the
        exclusive end of a CalDAV time-range filter). Naive datetimes are
        interpreted in the server's default timezone, matching
        `parse_datetime_input` everywhere else.
        """
        if value is None:
            return None
        parsed = mapping.parse_datetime_input(value)
        if isinstance(parsed, datetime):
            return parsed
        return mapping.local_midnight(parsed + timedelta(days=1) if exclusive_end else parsed)

    def _task_lists(self) -> list[tuple[str, DAVCalendar]]:
        """Return (display name, calendar) pairs for every VTODO task list on the account.

        Read from the cached collection listing (`_list_collections`), not
        re-listed per call. Named lists deliberately don't come through here:
        they are resolved one at a time by `_with_collection`, so an unknown
        name raises `TaskListNotFoundError` instead of being skipped and a
        typo can't silently produce an empty result.
        """
        calendars = self._list_collections()
        result: list[tuple[str, DAVCalendar]] = []
        for calendar in calendars:
            if not self._supports_component(calendar, "VTODO"):
                continue
            name = calendar.get_display_name() or str(calendar.url)
            result.append((name, calendar))
        return result

    def _event_calendars(self, calendar_names: list[str] | None) -> list[tuple[str, DAVCalendar]]:
        """Return (display name, calendar) pairs for the VEVENT calendars to query.

        With explicit `calendar_names`, each is resolved individually (going
        through the cache); unknown names raise `CalendarNotFoundError`
        instead of being skipped, so a typo can't silently produce an empty
        result. With `None`, every VEVENT-supporting calendar on the account
        is returned, freshly listed.
        """
        if calendar_names is not None:
            return [(name, self._get_collection(name, "VEVENT")) for name in calendar_names]

        calendars = self._list_collections()
        result: list[tuple[str, DAVCalendar]] = []
        for calendar in calendars:
            if not self._supports_component(calendar, "VEVENT"):
                continue
            name = calendar.get_display_name() or str(calendar.url)
            result.append((name, calendar))
        return result

    def list_calendars(self) -> list[dict[str, Any]]:
        """Return all VEVENT-supporting calendars as {"name", "url", "farbe", "komponenten"}.

        Task-only lists (VTODO) are excluded - `list_task_lists` is their
        counterpart. `komponenten` reports the full advertised component set
        so a mixed VEVENT+VTODO collection is recognizable as both.
        """
        with self._lock:
            calendars = self._list_collections()

            result: list[dict[str, Any]] = []
            for calendar in calendars:
                components = self._supported_components(calendar)
                if components and "VEVENT" not in components:
                    continue
                name = calendar.get_display_name() or str(calendar.url)
                # Component set and color both come from the batched metadata
                # (one PROPFIND for the whole home-set). Only a calendar
                # missing from that batch falls back to a per-calendar
                # color PROPFIND; the color is cosmetic, so a failure there
                # just yields no color rather than erroring out.
                farbe: str | None
                meta = self._collection_meta_for(calendar)
                if meta is not None:
                    farbe = meta["color"]
                else:
                    try:
                        props = calendar.get_properties([ical_elements.CalendarColor()])
                        raw = props.get(ical_elements.CalendarColor.tag)
                        farbe = str(raw) if raw else None
                    except Exception:
                        farbe = None
                result.append(
                    {
                        "name": name,
                        "url": str(calendar.url),
                        "farbe": farbe,
                        "komponenten": sorted(components),
                    }
                )
                if sum(1 for entry in result if entry["name"] == name) == 1:
                    self._cache_collection(
                        "VEVENT", name, calendar, fetched_at=self._collections_fetched_at
                    )
            # Drop cache entries that turned out to be ambiguous after all.
            counts: dict[str, int] = {}
            for entry in result:
                counts[str(entry["name"])] = counts.get(str(entry["name"]), 0) + 1
            for dup_name, count in counts.items():
                if count > 1:
                    self._calendar_cache.pop(("VEVENT", dup_name), None)
            return result

    def create_calendar(self, display_name: str, farbe: str | None = None) -> dict[str, Any]:
        """Create a new VEVENT calendar, optionally with a "#RRGGBB" color.

        Mirrors `create_task_list`'s conflict handling: a display-name clash
        with an existing event calendar, or a collection-id clash on the
        server (405/409 from MKCALENDAR), both fail loudly instead of
        silently reusing an existing calendar.
        """
        if not display_name or not display_name.strip():
            raise InvalidEventDataError("display_name is required to create a calendar.")
        if farbe is not None and not _COLOR_RE.match(farbe):
            raise InvalidEventDataError(
                f"farbe must look like '#RRGGBB' (or '#RRGGBBAA'), got '{farbe}'."
            )

        slug = _slugify(display_name)

        with self._lock:
            existing = self._list_collections(fresh=True)
            principal = self._get_principal()

            if any(
                calendar.get_display_name() == display_name
                and self._supports_component(calendar, "VEVENT")
                for calendar in existing
            ):
                raise CalendarAlreadyExistsError(
                    f"A calendar named '{display_name}' already exists."
                )

            calendar = self._make_collection(
                principal,
                display_name,
                slug,
                component="VEVENT",
                conflict_error=CalendarAlreadyExistsError,
                kind="calendar",
            )

            if farbe is not None:
                try:
                    calendar.set_properties([ical_elements.CalendarColor(farbe)])
                except Exception as exc:
                    raise _translate(exc) from exc

            self._invalidate_collection_caches()
            self._cache_collection("VEVENT", display_name, calendar, fetched_at=monotonic())
            return {"name": display_name, "url": str(calendar.url), "farbe": farbe}

    def delete_calendar(self, calendar_name: str) -> None:
        """Permanently delete an event calendar and every event inside it.

        Irreversible from this API's point of view (the server may keep a
        trashbin, but this client can't restore from it) - callers should
        confirm with the user first.
        """

        def op(calendar: DAVCalendar) -> None:
            calendar.delete()

        with self._lock:
            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            self._calendar_cache.pop(("VEVENT", calendar_name), None)
            self._invalidate_collection_caches()

    def update_calendar(
        self,
        calendar_name: str,
        new_display_name: str | None = None,
        farbe: str | None = None,
    ) -> dict[str, Any]:
        """Rename an event calendar and/or set its color (PROPPATCH).

        Only the display name/color change - the collection's URL/id stays
        stable, so clients that reference the calendar by URL are unaffected.
        Renaming to a name another event calendar already has raises
        `CalendarAlreadyExistsError`, mirroring `rename_task_list`.
        """
        if new_display_name is not None and not new_display_name.strip():
            raise InvalidEventDataError("new_display_name must not be empty.")
        if new_display_name is None and farbe is None:
            raise InvalidEventDataError("Nothing to update: give new_display_name and/or farbe.")
        if farbe is not None and not _COLOR_RE.match(farbe):
            raise InvalidEventDataError(
                f"farbe must look like '#RRGGBB' (or '#RRGGBBAA'), got '{farbe}'."
            )

        with self._lock:
            existing = self._list_collections(fresh=True)

            matches = [
                c
                for c in existing
                if c.get_display_name() == calendar_name and self._supports_component(c, "VEVENT")
            ]
            if not matches:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.")
            if len(matches) > 1:
                raise TaskMcpError(
                    f"Multiple calendars are named '{calendar_name}', which is ambiguous. "
                    "Rename the calendars in Nextcloud so each has a distinct name, or "
                    "use a different, unambiguous name."
                )
            calendar = matches[0]

            if (
                new_display_name is not None
                and new_display_name != calendar_name
                and any(
                    c.get_display_name() == new_display_name
                    and self._supports_component(c, "VEVENT")
                    for c in existing
                )
            ):
                raise CalendarAlreadyExistsError(
                    f"A calendar named '{new_display_name}' already exists."
                )

            props: list[Any] = []
            if new_display_name is not None:
                props.append(dav.DisplayName(new_display_name))
            if farbe is not None:
                props.append(ical_elements.CalendarColor(farbe))
            try:
                calendar.set_properties(props)
            except Exception as exc:
                raise _translate(exc) from exc

            final_name = new_display_name if new_display_name is not None else calendar_name
            self._calendar_cache.pop(("VEVENT", calendar_name), None)
            # A color change is reflected in the cached metadata, so drop it.
            self._invalidate_collection_caches()
            self._cache_collection("VEVENT", final_name, calendar, fetched_at=monotonic())
            return {"name": final_name, "url": str(calendar.url), "farbe": farbe}

    def list_events(
        self,
        calendar_names: list[str] | None = None,
        von: str | None = None,
        bis: str | None = None,
        suchtext: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        expand: bool = False,
        *,
        ohne_erinnerung: bool = False,
        ohne_sichtbarkeit: bool = False,
        ohne_tags: bool = False,
        uid_regex: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return events across one, several, or all VEVENT calendars, sorted by start.

        `von`/`bis` bound the query server-side (CalDAV time-range REPORT), so
        recurring events that have an occurrence in the window are matched
        even when their master event started long before it. With
        `expand=True`, recurring events are additionally expanded into their
        individual occurrences within the window (requires both bounds).
        `suchtext`/`tag`/`limit` - and the cleanup filters `ohne_erinnerung`/
        `ohne_sichtbarkeit`/`ohne_tags`/`uid_regex` - filter the parsed
        results client-side via `event_mapping.filter_events`.
        """
        with self._lock:
            events = self._collect_events(calendar_names, von, bis, expand)

        result = event_mapping.filter_events(
            events,
            suchtext=suchtext,
            tag=tag,
            limit=limit,
            ohne_erinnerung=ohne_erinnerung,
            ohne_sichtbarkeit=ohne_sichtbarkeit,
            ohne_tags=ohne_tags,
            uid_regex=uid_regex,
        )
        # "quelle_url" is collected alongside "kalender" for `get_agenda`'s
        # provenance (see `_collect_events`), but isn't part of this method's
        # documented return shape - only `get_agenda` keeps it.
        for item in result:
            item.pop("quelle_url", None)
        return result

    def _collect_events(
        self,
        calendar_names: list[str] | None,
        von: str | None,
        bis: str | None,
        expand: bool,
    ) -> list[dict[str, Any]]:
        """Query and parse VEVENTs from the target calendars.

        Shared by `list_events` and `get_agenda` so the two can never
        disagree about which events a given (calendar_names, von, bis,
        expand) selection matches - `get_agenda` applies the exact same
        query rather than re-deriving the logic. Each parsed dict carries
        "kalender" (the calendar's display name) and "quelle_url" (the
        CalDAV URL of the specific calendar it was actually read from - the
        *resolved* collection, so it reflects a stale-cache retry if one
        happened, see `_with_collection`). `list_events` strips "quelle_url"
        before returning; `get_agenda` keeps it, since that's what makes a
        returned event traceable to one specific calendar even when two
        calendars share a display name (the failure mode this was added
        for).

        Must be called with `self._lock` held, like every other CalDAV op.
        """
        start_bound = self._range_bound(von, exclusive_end=False)
        end_bound = self._range_bound(bis, exclusive_end=True)
        if expand and (start_bound is None or end_bound is None):
            raise InvalidEventDataError(
                "Expanding recurring events requires both von and bis bounds."
            )

        def op(calendar: DAVCalendar) -> tuple[list[Any], str]:
            if start_bound is None and end_bound is None:
                results = calendar.events()
            else:
                # caldav's search/expand path is only well-defined with
                # both ends present; widen an omitted side instead of
                # passing None through (see _RANGE_MIN/_RANGE_MAX).
                results = calendar.search(
                    start=start_bound or _RANGE_MIN,
                    end=end_bound or _RANGE_MAX,
                    event=True,
                    expand=expand,
                )
            return list(results), str(calendar.url)

        targets = self._event_calendars(calendar_names)
        events: list[dict[str, Any]] = []
        for name, target_calendar in targets:
            try:
                if calendar_names is not None:
                    # Named calendars go through the cache-aware path so a
                    # stale cache entry is re-resolved once (A3).
                    objs, source_url = self._with_collection(name, "VEVENT", op)
                else:
                    # The all-calendars case just listed everything fresh;
                    # querying the object directly also keeps two
                    # same-named calendars both reachable here.
                    objs, source_url = op(target_calendar)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            for obj in objs:
                parsed = event_mapping.parse_vevent(obj.icalendar_component)
                parsed["kalender"] = name
                parsed["quelle_url"] = source_url
                events.append(parsed)

        return events

    def get_event(self, calendar_name: str, event_uid: str) -> dict[str, Any]:
        """Return a single event, parsed into the server's German event dict."""
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.event_by_uid(event_uid)

            try:
                event_obj = self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            parsed = event_mapping.parse_vevent(event_obj.icalendar_component)
            parsed["kalender"] = calendar_name
            return parsed

    def create_event(self, calendar_name: str, fields: event_mapping.EventFields) -> str:
        """Create a new event in the given calendar and return its UID.

        If `fields.teilnehmer` adds attendees, ORGANIZER is set to the
        caller's own address (discovered lazily, see
        `_get_own_organizer_address`) - Nextcloud's CalDAV server then does
        server-side scheduling (iMIP invitation mails) once the event is
        saved with both ORGANIZER and ATTENDEEs present.
        """
        if fields.titel is None:
            raise InvalidEventDataError("titel is required to create an event.")
        if fields.start is None:
            raise InvalidEventDataError("start is required to create an event.")
        with self._lock:
            own_organizer = self._get_own_organizer_address() if fields.teilnehmer else None
            new_uid = str(uuid.uuid4())
            event = Event()
            event.add("uid", new_uid)
            event.add("dtstamp", datetime.now(timezone.utc))
            event_mapping.apply_event_fields(event, fields, own_organizer=own_organizer)

            vcal = Calendar()
            vcal.add("prodid", "-//nextcloud-task-mcp//EN")
            vcal.add("version", "2.0")
            _sync_vtimezones(vcal, event)
            vcal.add_component(event)
            ical_text = vcal.to_ical().decode("utf-8")

            def op(calendar: DAVCalendar):
                calendar.save_event(ical=ical_text)

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{calendar_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            return new_uid

    def update_event(
        self, calendar_name: str, event_uid: str, fields: event_mapping.EventFields
    ) -> None:
        """Update only the given (non-None) fields of an existing event.

        Same ORGANIZER-on-first-attendee and server-side-scheduling behavior
        as `create_event` when `fields.teilnehmer` sets attendees.
        """
        with self._lock:
            own_organizer = self._get_own_organizer_address() if fields.teilnehmer else None

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.apply_event_fields(
                    event_obj.icalendar_component, fields, own_organizer=own_organizer
                )
                _sync_vtimezones(event_obj.icalendar_instance, event_obj.icalendar_component)
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def respond_to_event(
        self,
        calendar_name: str,
        event_uid: str,
        antwort: str,
        kommentar: str | None = None,
    ) -> None:
        """Set the caller's own PARTSTAT on an event they were invited to (RSVP reply).

        The own ATTENDEE entry is found by comparing the event's ATTENDEEs
        against the caller's own CalDAV calendar-user-addresses (case-
        insensitive, "mailto:" ignored on both sides) - see
        `_get_own_calendar_user_addresses`. Raises `InvalidEventDataError` if
        none match (the caller isn't an attendee of this event). Saves the
        event afterwards; Nextcloud's CalDAV server then propagates the reply
        to the organizer as an iMIP/iTIP REPLY, the same server-side
        scheduling mechanism that sends the original invitations (see
        create_event/update_event).
        """
        partstat = event_mapping.response_label_to_partstat(antwort)
        with self._lock:
            own_addresses = self._get_own_calendar_user_addresses()

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.apply_own_attendee_response(
                    event_obj.icalendar_component, own_addresses, partstat, kommentar
                )
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    @staticmethod
    def _validate_event_patch(fields: event_mapping.EventFields) -> None:
        """Reject an empty or invalid patch before a single event is written.

        A batch must not stop halfway because the 40th event was the first to
        reveal a bad RRULE, so the patch is applied to a throwaway VEVENT
        first: every check `apply_event_fields` performs - unknown
        `felder_leeren` names, setting and clearing the same field, bad
        status/visibility/RRULE/date, an attendee without an email - happens
        there, on nothing. The probe carries a DTSTART because relative
        reminders validate against its presence.

        The probe's DTSTART deliberately matches the patch's own `ende` kind
        and lies far in the past: `apply_event_fields` checks DTSTART against
        DTEND, so a timed probe would reject an all-day `ende` that every
        real all-day event in the batch would have accepted. Whether the
        patch actually fits a *given* event is per-event and is reported per
        UID, not here.
        """
        patch_fields = [f.name for f in dataclasses.fields(fields) if f.name != "clear"]
        if not fields.clear and all(getattr(fields, name) is None for name in patch_fields):
            raise InvalidEventDataError(
                "No fields to update given - set at least one field, or name one in felder_leeren."
            )

        probe = Event()
        if fields.start is None:
            # With `start` in the patch, `apply_event_fields` sets DTSTART on
            # the probe itself before it validates anything against it.
            all_day_end = fields.ende is not None and _ALL_DAY_RE.match(fields.ende) is not None
            probe.add("dtstart", date(1970, 1, 1) if all_day_end else _PROBE_START)
        event_mapping.apply_event_fields(probe, fields)

    def _batch_over_uids(
        self,
        kind: _BatchKind,
        collection_name: str,
        uids: list[str],
        operation: Callable[[DAVCalendar, str], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Run `operation` per UID, reporting outcomes instead of aborting on the first failure.

        A dict returned by `operation` is merged into that UID's result entry,
        for a batch whose per-object outcome is more than "it worked"
        (`change_exdates` reports what it changed on each event, `move_tasks`
        which route the move took).

        A batch is only useful if one bad UID doesn't discard the work done
        for the others, so a failure that belongs to a single object (unknown
        UID, edit conflict, a patch that doesn't fit *this* one, a move that
        clashes in the target) becomes an entry in `ergebnisse` and the loop
        continues. Anything saying the whole call is broken - bad
        credentials, transport failure, the collection itself gone - still
        propagates, because continuing would just produce one identical
        error per UID.

        Each item is run through `_retry_transient`, so a proxy 502 or a
        dropped connection costs a retry rather than an entry in the failure
        list. Only a failure that survives those retries is reported.

        The collection is resolved once for the whole batch. If that cached
        collection turns out to be stale, resolution is refreshed once (the
        same recovery `_with_collection` does per call) rather than reporting
        every UID as missing.
        """
        if not uids:
            raise kind.invalid_error(
                f"{kind.uids_param} must not be empty - name at least one "
                f"{kind.noun.lower()} to act on."
            )
        unique_uids = _dedup_strings(uids) or []
        if len(unique_uids) > _BATCH_UID_LIMIT:
            raise kind.invalid_error(
                f"A batch takes at most {_BATCH_UID_LIMIT} {kind.noun.lower()} UIDs, "
                f"got {len(unique_uids)}. Split the call into several smaller ones."
            )

        with self._lock:
            try:
                collection = self._get_collection(collection_name, kind.component)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise self._not_found(collection_name, kind.component) from exc
            except Exception as exc:
                raise _translate(exc) from exc

            refreshed = False
            results: list[dict[str, Any]] = []
            succeeded = 0
            failed = 0

            for uid in unique_uids:
                detail: dict[str, Any] | None = None
                try:
                    try:
                        detail = _retry_transient(functools.partial(operation, collection, uid))
                    except caldav_error.NotFoundError:
                        if refreshed:
                            raise
                        # Either this one object is gone or the whole cached
                        # collection is stale - re-resolve once and retry, so
                        # a stale cache can't turn into "all 60 UIDs missing".
                        refreshed = True
                        self._calendar_cache.pop((kind.component, collection_name), None)
                        self._invalidate_collection_caches()
                        collection = self._resolve_and_cache(collection_name, kind.component)
                        detail = _retry_transient(functools.partial(operation, collection, uid))
                except caldav_error.NotFoundError:
                    results.append(
                        {
                            "uid": uid,
                            "status": "fehler",
                            "fehler": f"{kind.noun} '{uid}' was not found.",
                        }
                    )
                    failed += 1
                except Exception as exc:
                    translated = exc if isinstance(exc, TaskMcpError) else _translate(exc)
                    if isinstance(translated, TaskConflictError):
                        # `_translate` phrases this one for a single task; in a
                        # batch the UID is what tells the caller which one.
                        reason = (
                            f"{kind.noun} '{uid}' was modified by another client since it was "
                            "last read (conflicting edit). Re-read it and retry."
                        )
                    elif isinstance(translated, (kind.invalid_error, ObjectMoveError)):
                        # The patch itself was validated up front, so this is
                        # about *this* object - typically an all-day event
                        # meeting a timed patch, or a move that found the UID
                        # already sitting in the target. One mismatched object
                        # must not abort a batch that is already half written.
                        reason = str(translated)
                    else:
                        raise _aborted_batch(translated, kind, uid, results, unique_uids) from exc
                    results.append({"uid": uid, "status": "fehler", "fehler": reason})
                    failed += 1
                    continue
                else:
                    results.append({"uid": uid, "status": "ok", **(detail or {})})
                    succeeded += 1

            return {
                kind.name_key: collection_name,
                "erfolgreich": succeeded,
                "fehlgeschlagen": failed,
                "ergebnisse": results,
            }

    def _batch_over_events(
        self,
        calendar_name: str,
        event_uids: list[str],
        operation: Callable[[DAVCalendar, str], dict[str, Any] | None],
    ) -> dict[str, Any]:
        """Event flavour of `_batch_over_uids`, kept for the VEVENT call sites."""
        return self._batch_over_uids(_EVENT_BATCH, calendar_name, event_uids, operation)

    def update_events(
        self, calendar_name: str, event_uids: list[str], fields: event_mapping.EventFields
    ) -> dict[str, Any]:
        """Apply one field patch to several events of a calendar.

        The patch is validated before the first write (`_validate_event_patch`),
        so a rejected patch leaves every event untouched instead of stopping
        halfway through the batch. Per-event outcomes follow
        `_batch_over_events`.
        """
        self._validate_event_patch(fields)

        with self._lock:
            # Looked up once for the whole batch - it costs a principal
            # request and is the same address for every event.
            own_organizer = self._get_own_organizer_address() if fields.teilnehmer else None

            def operation(calendar: DAVCalendar, uid: str) -> None:
                event_obj = calendar.event_by_uid(uid)
                event_mapping.apply_event_fields(
                    event_obj.icalendar_component, fields, own_organizer=own_organizer
                )
                _sync_vtimezones(event_obj.icalendar_instance, event_obj.icalendar_component)
                event_obj.save()

            return self._batch_over_events(calendar_name, event_uids, operation)

    def change_exdates(
        self,
        calendar_name: str,
        event_uids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
        ignore_non_occurrences: bool = True,
    ) -> dict[str, Any]:
        """Add and/or remove exception dates on several event series at once.

        The additive counterpart of `update_events` with `ausnahme_daten`,
        which replaces each event's whole EXDATE set and therefore has to be
        handed every value the series already had. Here each event is read,
        merged and written server-side, so cancelling the same days across
        five series costs one call carrying those days - not five reads plus
        five rewrites of everything those series already skipped.

        An event whose stored set already covers the additions (and holds none
        of the removals) is left untouched rather than written back unchanged.
        Per-event outcomes follow `_batch_over_events`, each carrying the
        `apply_exdate_changes` report for that event.
        """
        if not add and not remove:
            raise InvalidEventDataError("Name at least one exception date to add or remove.")

        def operation(calendar: DAVCalendar, uid: str) -> dict[str, Any]:
            event_obj = calendar.event_by_uid(uid)
            component = event_obj.icalendar_component
            report = event_mapping.apply_exdate_changes(
                component,
                add=add,
                remove=remove,
                ignore_non_occurrences=ignore_non_occurrences,
            )
            if report["added"] or report["removed"]:
                _sync_vtimezones(event_obj.icalendar_instance, component)
                event_obj.save()
            return report

        return self._batch_over_events(calendar_name, event_uids, operation)

    def delete_events(self, calendar_name: str, event_uids: list[str]) -> dict[str, Any]:
        """Permanently delete several events of a calendar.

        Irreversible from this API's point of view, like `delete_event`, and
        a batch multiplies the damage a wrong UID list does - callers should
        confirm with the user first. Per-event outcomes follow
        `_batch_over_events`.
        """

        def operation(calendar: DAVCalendar, uid: str) -> None:
            calendar.event_by_uid(uid).delete()

        return self._batch_over_events(calendar_name, event_uids, operation)

    def delete_event(self, calendar_name: str, event_uid: str) -> None:
        """Permanently delete an event."""
        with self._lock:

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_obj.delete()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def move_event(
        self,
        calendar_name: str,
        event_uid: str,
        target_calendar: str,
        verknuepfte_aufgabe: str | None = None,
        clear: tuple[str, ...] | list[str] = (),
    ) -> dict[str, str]:
        """Move an event from one calendar to another, optionally re-linking its task.

        Prefers a CalDAV MOVE request (preserving server-side URL identity and
        ETags). Falls back to copy-then-delete if the server rejects MOVE with
        403/405/409/501, and retries it instead if the server gives no answer
        at all (502/503/504).

        Args:
            calendar_name: Display name of the source calendar.
            event_uid: UID of the event to move.
            target_calendar: Display name of the target calendar.
            verknuepfte_aufgabe: Optional task UID to link the event to, applied
                in the *target* calendar once the move succeeded. As in
                `update_event`, this replaces the event's whole RELATED-TO set
                (so any "voraussetzung" link goes with it) rather than adding one
                - `link_task_to_event` is the additive counterpart.
            clear: Optional `("verknuepfte_aufgabe",)` to drop the event's task
                links instead. Any other field name raises
                `InvalidEventDataError`; use `update_event` for those.

        Returns:
            {"uid": event_uid, "von": source, "nach": target,
            "methode": "MOVE" | "kopiert" | "bereits_dort"}, plus
            "hierarchie": "gesetzt" | "geleert" when the link was changed.
        """
        clear_link = _validate_move_clear(
            clear,
            _MOVE_EVENT_CLEARABLE,
            verknuepfte_aufgabe,
            InvalidEventDataError,
        )

        with self._lock:
            result = self._move_object(calendar_name, event_uid, target_calendar, "VEVENT")
            if verknuepfte_aufgabe is None and not clear_link:
                return result

            fields = event_mapping.EventFields(
                verknuepfte_aufgabe=verknuepfte_aufgabe,
                clear=(_MOVE_EVENT_CLEARABLE,) if clear_link else (),
            )
            self._apply_move_hierarchy(
                lambda: self.update_event(target_calendar, event_uid, fields),
                uid=event_uid,
                target_display=result["nach"],
                kind_article="Event",
                field_name=_MOVE_EVENT_CLEARABLE,
                retry_tool="update_event",
            )
            result["hierarchie"] = "geleert" if clear_link else "gesetzt"
            return result

    @staticmethod
    def _fetch_object(collection: DAVCalendar, uid: str, component: str) -> Any:
        """Read one calendar object of either kind from `collection` by UID."""
        if component == "VEVENT":
            return collection.event_by_uid(uid)
        return collection.get_todo_by_uid(uid)

    def _move_object(
        self, source_name: str, uid: str, target_name: str, component: str
    ) -> dict[str, str]:
        """Move a calendar object (VEVENT or VTODO) from one collection to another.

        A move that fails transiently (a gateway status, a dropped connection)
        is retried whole - see `_retry_transient` and `_move_one`'s
        "bereits_dort" recovery, which together make a lost acknowledgement
        cost a second request rather than a misleading error.
        """
        with self._lock:
            target_col = self._resolve_target_collection(target_name, component)
            source_col = self._resolve_collection(source_name, component)
            source_display = source_col.get_display_name() or source_name
            target_display = target_col.get_display_name() or target_name
            try:
                return _retry_transient(
                    functools.partial(
                        self._move_one,
                        source_col,
                        target_col,
                        uid,
                        component,
                        source_display,
                        target_display,
                    )
                )
            except caldav_error.NotFoundError as exc:
                # `_move_one` deliberately leaves this untranslated so a batch
                # can tell "this UID is gone" from "the cached collection is
                # stale" (see `_batch_over_uids`); a single move has no such
                # recovery and just reports the object as missing.
                if component == "VEVENT":
                    raise EventNotFoundError(f"Event '{uid}' was not found.") from exc
                raise TaskNotFoundError(f"Task '{uid}' was not found.") from exc

    def _move_one(
        self,
        source_col: DAVCalendar,
        target_col: DAVCalendar,
        uid: str,
        component: str,
        source_display: str,
        target_display: str,
    ) -> dict[str, str]:
        """Move one object between two collections that are already resolved.

        Split out of `_move_object` so a batch move resolves both collections
        once and then walks its UID list through here, instead of paying two
        name resolutions per task.

        Every failure that concerns only this one object is raised as an
        `ObjectMoveError` naming the state it was left in; a missing source
        object is left as caldav's own `NotFoundError` for the caller to phrase
        (see `_move_object`). Failures that concern the whole connection - bad
        credentials above all - propagate as themselves.
        """
        with self._lock:
            source_url_norm = _normalize_collection_href(str(source_col.url))
            target_url_norm = _normalize_collection_href(str(target_col.url))

            if source_url_norm == target_url_norm:
                return {
                    "uid": uid,
                    "von": source_display,
                    "nach": target_display,
                    "methode": "MOVE",
                }

            kind_str = "event" if component == "VEVENT" else "task"
            kind_label = "calendar" if component == "VEVENT" else "task list"
            kind_article = "An event" if component == "VEVENT" else "A task"

            def fetch_from_target() -> Any:
                return self._fetch_object(target_col, uid, component)

            try:
                obj = self._fetch_object(source_col, uid, component)
            except caldav_error.NotFoundError as not_in_source:
                # A MOVE the server carried out but never got to acknowledge -
                # a proxy 502 on the way back, a dropped connection - leaves
                # the source empty and the target holding the object. A retry
                # of this move (see `_retry_transient`) lands exactly here, so
                # check the target before calling the UID missing: this is what
                # makes re-running a half-failed batch converge instead of
                # reporting every already-moved task as gone.
                try:
                    fetch_from_target()
                except caldav_error.NotFoundError:
                    raise not_in_source from None
                except TaskMcpError:
                    raise
                except Exception as probe_exc:
                    raise _translate(probe_exc) from probe_exc
                return {
                    "uid": uid,
                    "von": source_display,
                    "nach": target_display,
                    "methode": "bereits_dort",
                }
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            resource_name = str(obj.url).rstrip("/").split("/")[-1]
            target_url_str = str(target_col.url)
            if not target_url_str.endswith("/"):
                target_url_str += "/"
            destination = target_url_str + resource_name

            use_fallback = False
            try:
                # Deliberately NOT routed through `_dav_request`: that helper
                # turns caldav's AuthorizationError into one flat message, and
                # caldav collapses HTTP 401 and 403 into that same exception
                # (see `_translate`). Here the difference decides what happens
                # next - a 403 means "this server won't MOVE between
                # collections", which is exactly the case the copy fallback
                # exists for, while a 401 means the credentials are wrong and
                # retrying as a copy would only produce a misleading error.
                # `.reason` is the one field that still tells them apart.
                response = self._client.request(
                    str(obj.url),
                    "MOVE",
                    "",
                    {"Destination": destination, "Overwrite": "F"},
                )
            except caldav_error.AuthorizationError as exc:
                # `.reason` is only set when caldav was given one; treat a
                # missing reason as "not provably a 403" so an ambiguous
                # failure never starts writing copies.
                if (getattr(exc, "reason", "") or "").strip().lower() == "forbidden":
                    use_fallback = True
                else:
                    raise AuthenticationFailedError(
                        "Nextcloud rejected the CalDAV credentials (check username/app password)."
                    ) from exc
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc
            else:
                status = getattr(response, "status", None)
                if status in (200, 201, 204):
                    return {
                        "uid": uid,
                        "von": source_display,
                        "nach": target_display,
                        "methode": "MOVE",
                    }
                if status == 412:
                    # `Overwrite: F` - the target already holds this UID. Not
                    # an ETag conflict (`TaskConflictError`), which is about a
                    # concurrent edit to the *same* object.
                    raise ObjectMoveError(
                        f"{kind_article} with UID '{uid}' already exists in target "
                        f"{kind_label} '{target_display}'."
                    )
                if status in _TRANSIENT_HTTP_STATUSES:
                    # Deliberately NOT the copy fallback, which is for a server
                    # that *won't* MOVE (403/405/409/501). A gateway status is
                    # no answer at all: the MOVE may well have been carried out
                    # and only its acknowledgement lost, in which case copying
                    # would read a source that is already gone or clash with
                    # the object now sitting in the target. Retrying the whole
                    # move is the answer instead - its source lookup then finds
                    # the object in the target and reports "bereits_dort".
                    raise TransientServerError(
                        f"Nextcloud gave no answer while moving {kind_str} '{uid}' to "
                        f"'{target_display}' (HTTP {status}); the move may or may not "
                        "have been carried out. Check both collections."
                    )
                if status in (403, 405, 409, 501):
                    use_fallback = True
                else:
                    raise ObjectMoveError(
                        f"Nextcloud rejected moving {kind_str} '{uid}' (HTTP {status})."
                    )

            if use_fallback:
                # `Overwrite: F` would have stopped a server-side MOVE from
                # replacing an existing object; the copy path has to make that
                # check itself, before writing anything.
                try:
                    fetch_from_target()
                except caldav_error.NotFoundError:
                    pass
                except TaskMcpError:
                    raise
                except Exception as exc:
                    raise _translate(exc) from exc
                else:
                    raise ObjectMoveError(
                        f"{kind_article} with UID '{uid}' already exists in target "
                        f"{kind_label} '{target_display}'."
                    )

                # The whole calendar object, not just the component this server
                # parses: VTIMEZONEs, VALARMs and any RECURRENCE-ID override
                # instances travel with it.
                try:
                    ical_text = obj.icalendar_instance.to_ical().decode("utf-8")
                except Exception as read_exc:
                    raise ObjectMoveError(
                        f"Could not read {kind_str} '{uid}' from '{source_display}' to copy it. "
                        "Nothing was written or deleted."
                    ) from read_exc

                try:
                    # `no_overwrite=True` makes caldav re-check for an existing
                    # object right before the PUT. It doesn't close the window
                    # the pre-check above leaves open (that check is client-side
                    # too), but it shrinks it, and it means a future caller
                    # reaching this write without the pre-check still can't
                    # clobber a target object.
                    if component == "VEVENT":
                        target_col.save_event(ical=ical_text, no_overwrite=True)
                    else:
                        target_col.save_todo(ical=ical_text, no_overwrite=True)
                except caldav_error.ConsistencyError as clash_exc:
                    raise ObjectMoveError(
                        f"{kind_article} with UID '{uid}' already exists in target "
                        f"{kind_label} '{target_display}'. The original in "
                        f"'{source_display}' was left untouched."
                    ) from clash_exc
                except TaskMcpError:
                    raise
                except Exception as write_exc:
                    if _is_transient(write_exc):
                        # Deliberately not retried, and deliberately not
                        # claiming the target is clean. A write that got no
                        # answer may still have landed, and a retry would
                        # either find its own copy and call it a clash, or -
                        # worse, if it ever learned to ignore that - risk
                        # deleting the source against a copy nobody verified.
                        raise ObjectMoveError(
                            f"Copying {kind_str} '{uid}' to '{target_display}' got no answer "
                            f"from the server, so it may or may not have arrived there. The "
                            f"original in '{source_display}' was kept either way - check "
                            f"'{target_display}' before retrying."
                        ) from write_exc
                    raise ObjectMoveError(
                        f"Could not copy {kind_str} '{uid}' to '{target_display}'. "
                        f"The original in '{source_display}' was left untouched."
                    ) from write_exc

                # Read it back before deleting anything: a write the server
                # accepted but did not persist must not cost the original.
                try:
                    copied = fetch_from_target()
                except TaskMcpError:
                    raise
                except Exception as verify_exc:
                    raise ObjectMoveError(
                        f"Copied {kind_str} '{uid}' to '{target_display}', but could not read "
                        f"it back to confirm the copy, so the original in '{source_display}' "
                        "was kept. Check both collections before retrying."
                    ) from verify_exc

                # The UID resolving in the target only proves *something*
                # arrived. For a recurring object the overrides are separate
                # components under the same UID, so compare them explicitly.
                expected_markers = _instance_markers(obj, component)
                if expected_markers is None:
                    # Nothing to compare against - "can't tell" is not a licence
                    # to delete, even though the copy itself may be fine.
                    raise ObjectMoveError(
                        f"Copied {kind_str} '{uid}' to '{target_display}', but could not re-read "
                        f"the original in '{source_display}' to compare instances, so it was "
                        f"kept. Remove the copy from '{target_display}' before retrying."
                    )
                if expected_markers:
                    copied_markers = _instance_markers(copied, component)
                    # An unreadable copy counts as an empty one: "can't tell"
                    # must never license deleting the source.
                    missing = sum(
                        max(count - (copied_markers[marker] if copied_markers else 0), 0)
                        for marker, count in expected_markers.items()
                    )
                    if missing:
                        total = sum(expected_markers.values())
                        raise ObjectMoveError(
                            f"Copied {kind_str} '{uid}' to '{target_display}', but {missing} of "
                            f"{total} instances are missing there, so the original in "
                            f"'{source_display}' was kept. Remove the incomplete copy from "
                            f"'{target_display}' before retrying."
                        )

                try:
                    obj.delete()
                except TaskMcpError:
                    raise
                except Exception as del_exc:
                    raise ObjectMoveError(
                        f"Copied {kind_str} '{uid}' to '{target_display}', but deleting the "
                        f"original from '{source_display}' failed - it now exists in both "
                        "collections. Delete the original manually."
                    ) from del_exc

                return {
                    "uid": uid,
                    "von": source_display,
                    "nach": target_display,
                    "methode": "kopiert",
                }

            # Unreachable: every branch above either returns, raises, or sets
            # use_fallback. Kept so the function has no implicit None return.
            raise ObjectMoveError(f"Could not move {kind_str} '{uid}'.")

    # ------------------------------------------------------------------
    # Task <-> event linking and combined views
    # ------------------------------------------------------------------

    def link_task_to_event(
        self,
        list_name: str,
        task_uid: str,
        calendar_name: str,
        event_uid: str,
        beziehung: str = "zeitblock",
    ) -> None:
        """Link a task (VTODO) to an event (VEVENT) via RELATED-TO on the event.

        The RELATED-TO property is written on the *event*, never the task:
        Nextcloud Tasks interprets a task's RELATED-TO as "subtask of", so
        pointing one at an event UID would garble the task tree in its UI,
        while the calendar app simply ignores the property (it round-trips
        as raw data). See `_LINK_RELTYPES` for the two supported semantics.
        """
        reltype = _LINK_RELTYPES.get(beziehung)
        if reltype is None:
            raise InvalidEventDataError(
                f"Unknown beziehung '{beziehung}'. Expected one of: {', '.join(_LINK_RELTYPES)}."
            )

        with self._lock:
            # Verify the task actually exists before writing its UID onto the
            # event - a dangling link would be invisible until someone tried
            # to follow it.
            def check_task(calendar: DAVCalendar):
                calendar.get_todo_by_uid(task_uid)

            try:
                self._with_collection(list_name, "VTODO", check_task)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            def op(calendar: DAVCalendar):
                event_obj = calendar.event_by_uid(event_uid)
                event_mapping.add_relation(event_obj.icalendar_component, task_uid, reltype)
                event_obj.save()

            try:
                self._with_collection(calendar_name, "VEVENT", op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise EventNotFoundError(f"Event '{event_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

    def list_events_for_task(
        self,
        list_name: str,
        task_uid: str,
        calendar_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return events linked to the given task - the task-side counterpart of link_task_to_event.

        The RELATED-TO link is only ever written on the event (see
        `link_task_to_event`'s docstring for why), so there is no CalDAV
        query that starts from a task UID and finds the events pointing at
        it: every event in the queried calendars has to be fetched and its
        parsed `verknuepfte_aufgaben` checked for `task_uid`. Verifies the
        task exists first, same check and error as `link_task_to_event`.
        """
        with self._lock:

            def check_task(calendar: DAVCalendar):
                calendar.get_todo_by_uid(task_uid)

            try:
                self._with_collection(list_name, "VTODO", check_task)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskNotFoundError(f"Task '{task_uid}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            def op(calendar: DAVCalendar):
                return calendar.events()

            targets = self._event_calendars(calendar_names)
            events: list[dict[str, Any]] = []
            for name, target_calendar in targets:
                try:
                    if calendar_names is not None:
                        # Named calendars go through the cache-aware path so a
                        # stale cache entry is re-resolved once (A3).
                        objs = self._with_collection(name, "VEVENT", op)
                    else:
                        # The all-calendars case just listed everything fresh;
                        # querying the object directly also keeps two
                        # same-named calendars both reachable here.
                        objs = op(target_calendar)
                except TaskMcpError:
                    raise
                except caldav_error.NotFoundError as exc:
                    raise CalendarNotFoundError(f"Calendar '{name}' was not found.") from exc
                except Exception as exc:
                    raise _translate(exc) from exc

                for obj in objs:
                    parsed = event_mapping.parse_vevent(obj.icalendar_component)
                    if any(rel["uid"] == task_uid for rel in parsed["verknuepfte_aufgaben"]):
                        parsed["kalender_name"] = name
                        events.append(parsed)

            events.sort(key=event_mapping._start_sort_key)
            return events

    def create_event_from_task(
        self,
        list_name: str,
        task_uid: str,
        calendar_name: str,
        start: str | None = None,
        dauer_minuten: int | None = None,
        ende: str | None = None,
        beschreibung: str | None = None,
        erinnerungen: list[str] | None = None,
        sichtbarkeit: str | None = None,
    ) -> str:
        """Create a calendar event from an existing task (timeboxing) and link them.

        Title, location and tags are always copied from the task; the event
        starts at `start` (or, if omitted, the task's due date/time). A
        date-only start produces a one-day all-day event instead - `ende`
        must then also be a date, or absent (both left to `create_event`'s
        own `_check_start_end_consistency`, not duplicated here).

        The event's length is `ende` (an explicit end) XOR `dauer_minuten` (a
        length in minutes from `start`) - passing both is rejected, since
        they'd disagree about where the event ends. With neither, the event
        runs 60 minutes (`dauer_minuten` defaults to `None` rather than `60`
        so a call passing neither can tell "not given" from "given as 60" -
        both currently behave the same way, but only the former is meant to
        keep doing so if the default ever changes). `dauer_minuten` is
        ignored for an all-day start, same as before.

        `beschreibung` overrides the task's `notizen` for the event's
        description; `None` (the default) inherits `notizen` unchanged, while
        an explicit `""` clears it - `is not None`, not truthiness, decides
        which happened. `erinnerungen`/`sichtbarkeit` pass straight through
        to the new event.

        The new event carries RELATED-TO;RELTYPE=PARENT with the task's UID
        (the "zeitblock" link semantics).
        """
        if ende is not None and dauer_minuten is not None:
            raise InvalidEventDataError(
                "ende and dauer_minuten cannot both be given; pass at most one to "
                "control how long the event runs."
            )
        if dauer_minuten is not None and dauer_minuten <= 0:
            raise InvalidEventDataError(f"dauer_minuten must be > 0, got {dauer_minuten}.")

        with self._lock:
            task = self.get_task(list_name, task_uid)

            start_spec = start if start is not None else task.get("faellig_datum")
            if start_spec is None:
                raise InvalidEventDataError(
                    "The task has no faellig_datum (due date); pass an explicit start "
                    "for the event instead."
                )

            parsed_start = mapping.parse_datetime_input(start_spec, keep_zone=True)
            if isinstance(parsed_start, datetime) and start is None:
                # A task's due date is stored as a bare UTC instant (VTODOs
                # keep no zone) and read back with a numeric offset, so there
                # is no zone left to carry over - but the wall clock the caller
                # originally typed was in the server's default zone, and a
                # timebox should look like the event that same value would
                # produce through `create_event`. An explicit `start` argument
                # keeps `create_event`'s own rules instead: a named zone is
                # preserved below, a numeric offset stays UTC.
                parsed_start = parsed_start.astimezone(mapping.get_default_timezone())
            if isinstance(parsed_start, datetime):
                start_value = _zone_preserving_isoformat(parsed_start)
                if ende is not None:
                    ende_value = ende
                else:
                    # `dauer_minuten`/the 60-minute default is a real duration:
                    # adding it to a zone-aware datetime would do wall-clock
                    # arithmetic, stretching a block that spans a DST change by
                    # the transition's own hour.
                    effective_minutes = dauer_minuten if dauer_minuten is not None else 60
                    parsed_end = (
                        parsed_start.astimezone(timezone.utc) + timedelta(minutes=effective_minutes)
                    ).astimezone(parsed_start.tzinfo)
                    ende_value = _zone_preserving_isoformat(parsed_end)
            else:
                # All-day due date -> one-day all-day event (inclusive end),
                # unless an explicit ende overrides it; dauer_minuten has no
                # meaning for an all-day event and is ignored either way.
                start_value = parsed_start.isoformat()
                ende_value = ende if ende is not None else start_value

            beschreibung_value = beschreibung if beschreibung is not None else task.get("notizen")

            fields = event_mapping.EventFields(
                titel=task.get("titel") or "Aufgabe",
                start=start_value,
                ende=ende_value,
                beschreibung=beschreibung_value,
                ort=task.get("ort"),
                tags=task.get("tags") or None,
                wiederholung=task.get("wiederholung"),
                erinnerungen=erinnerungen,
                sichtbarkeit=sichtbarkeit,
                verknuepfte_aufgabe=task_uid,
            )
            return self.create_event(calendar_name, fields)

    def get_agenda(
        self,
        datum: str,
        calendar_names: list[str] | None = None,
        list_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return one day's events and due tasks together (a combined agenda).

        CalDAV has no single query spanning VEVENTs and VTODOs, so this is
        plain server-side composition: a time-range event query (recurring
        events expanded to that day's occurrences) plus a due-date-filtered
        task listing per VTODO list. `datum` must be a date-only "YYYY-MM-DD"
        string; day boundaries are local days in the server's default timezone
        (`MCP_DEFAULT_TIMEZONE`), consistent with the rule used everywhere else
        in this server, and applied identically to the events and the tasks.

        Unlike `list_events`/`list_tasks`, every returned entry also carries a
        "quelle_url" key - the CalDAV URL of the exact collection (calendar or
        task list) it came from, alongside the existing "kalender"/"liste"
        display name. A display name alone can't tell two same-named
        collections apart (Nextcloud doesn't enforce uniqueness), so it's not
        enough to trace a surprising agenda entry back to a real collection;
        the URL is unambiguous. This is what makes a future "agenda shows
        something no other tool can find" report traceable to one specific
        collection instead of a guess. (Tasks additionally retain their
        "liste_url" from `list_tasks`).

        Because a CalDAV time-range REPORT resolves all-day and floating values
        in the *calendar collection's* timezone (RFC 4791 9.9) - the Nextcloud
        account's setting, which need not be `MCP_DEFAULT_TIMEZONE` - the event
        query covers the neighbouring days as well and the local day is cut out
        of the result here (`event_mapping.events_in_window`). Otherwise a
        neighbouring day's all-day event can appear in the agenda, or a
        floating one an hour before midnight go missing, purely from the two
        zones disagreeing.
        """
        parsed = mapping.parse_datetime_input(datum)
        if isinstance(parsed, datetime):
            raise InvalidEventDataError(
                f"datum must be a date-only 'YYYY-MM-DD' string, got '{datum}'."
            )

        day_start, day_end = event_mapping.local_day_window(parsed)

        with self._lock:
            self._ensure_collections()
            self._ttl_frozen = True
            try:
                raw_events = self._collect_events(
                    calendar_names,
                    von=(parsed - timedelta(days=1)).isoformat(),
                    bis=(parsed + timedelta(days=1)).isoformat(),
                    expand=True,
                )
                filtered_events = event_mapping.filter_events(
                    raw_events, suchtext=None, tag=None, limit=None
                )
                termine = event_mapping.events_in_window(filtered_events, day_start, day_end)

                if list_names is None:
                    raw_tasks = self._tasks_from_every_list(only_open=True)
                else:
                    raw_tasks = self._tasks_from_named_lists(list_names, only_open=True)

                aufgaben = mapping.filter_tasks(
                    raw_tasks,
                    due_before=datum,
                    due_after=datum,
                    prioritaet=None,
                    tag=None,
                    suchtext=None,
                    limit=None,
                )
            finally:
                self._ttl_frozen = False

            for task in aufgaben:
                task["quelle_url"] = task["liste_url"]

            return {"datum": parsed.isoformat(), "termine": termine, "aufgaben": aufgaben}

    def list_tags(
        self,
        calendar_names: list[str] | None = None,
        list_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return unique tags (CATEGORIES) and counts across calendars and task lists.

        Aggregates VEVENT and VTODO components together. Completed tasks are included
        (`include_completed=True`), so tags do not disappear when all tasks with that
        tag are completed.

        Tag matching is case-insensitive. Of the spellings found, the most common
        one is reported, alphabetically first on a tie - deliberately *not* "the
        first one seen", which would depend on the order the server happens to
        return collections in and could differ between two identical calls.

        The returned list is sorted by `anzahl` descending, with ties broken
        alphabetically by `tag` (case-insensitively, so a capitalized tag does
        not jump ahead of every lowercase one).

        `calendar_names` and `list_names` control which collections are queried:
        - `None` (default): query all VEVENT calendars / all VTODO task lists.
        - `[]`: query no VEVENT calendars / no VTODO task lists.
        - `["name1", ...]`: query specific named calendars / task lists.

        A mixed VEVENT+VTODO collection contributes its events and its tasks, each
        counted once: the two queries select disjoint component kinds, and a name
        repeated in the same argument is deduplicated before querying, so no
        component can be counted twice.

        WARNING: This call reads each target collection completely without a time
        window, which makes it an expensive operation - and it holds the service
        lock throughout, so every other tool call waits on it.
        """
        if isinstance(calendar_names, str):
            calendar_names = [calendar_names]
        if isinstance(list_names, str):
            list_names = [list_names]

        with self._lock:
            # An empty list means "none of that kind" to both collectors, so it
            # needs no special case here - unlike `None`, which means "all".
            events = self._collect_events(
                _dedup_strings(calendar_names), von=None, bis=None, expand=False
            )
            if list_names is None:
                tasks = self._tasks_from_every_list(only_open=False)
            else:
                tasks = self._tasks_from_named_lists(list_names, only_open=False)

            # Folded for counting - "Arbeit" and "arbeit" are one tag to
            # Nextcloud's UI too - but every spelling is kept so the reported
            # one can be picked deterministically below.
            spellings: dict[str, Counter[str]] = {}
            for entry in (*events, *tasks):
                for tag in entry.get("tags") or []:
                    spellings.setdefault(tag.lower(), Counter())[tag] += 1

            tag_entries: list[tuple[str, int]] = [
                (
                    min(variants.items(), key=lambda item: (-item[1], item[0]))[0],
                    sum(variants.values()),
                )
                for variants in spellings.values()
            ]
            # Folded for the tie-break too: sorting on the display spelling
            # would put every capitalized tag before every lowercase one.
            tag_entries.sort(key=lambda item: (-item[1], item[0].lower()))
            return [{"tag": tag, "anzahl": count} for tag, count in tag_entries]

    # ------------------------------------------------------------------
    # Free-busy
    # ------------------------------------------------------------------

    def get_free_busy(self, von: str, bis: str, benutzer: str | None = None) -> dict[str, Any]:
        """Return merged busy intervals in [von, bis] for the caller, or another user.

        With `benutzer=None`, busy blocks are computed from every VEVENT
        calendar the caller can see: events in range are fetched the same way
        as `list_events` (a server-side CalDAV time-range REPORT), then
        non-cancelled, non-transparent ones contribute a busy interval (see
        `event_mapping.event_busy_interval`), merged and sorted (see
        `event_mapping.merge_busy_intervals`).

        With `benutzer` set, a CalDAV RFC 6638 free-busy scheduling request is
        sent to the server (`principal.freebusy_request`, POSTing a VFREEBUSY
        to the schedule-outbox) asking about that user - the server resolves
        `benutzer` against its own known accounts, not this client. If the
        server rejects the request (unknown user, scheduling disabled, ...)
        this raises a `TaskMcpError` with a clean, actionable message.
        """
        start_bound = self._range_bound(von, exclusive_end=False)
        end_bound = self._range_bound(bis, exclusive_end=True)
        if start_bound is None or end_bound is None:
            raise InvalidEventDataError("von and bis are required for get_free_busy.")

        with self._lock:
            if benutzer is None:
                merged = self._own_free_busy(start_bound, end_bound)
            else:
                merged = self._free_busy_for_user(benutzer, start_bound, end_bound)

        return {
            "von": mapping.format_datetime_output(start_bound),
            "bis": mapping.format_datetime_output(end_bound),
            "benutzer": benutzer,
            "belegt": [
                {
                    "von": mapping.format_datetime_output(start),
                    "bis": mapping.format_datetime_output(end),
                }
                for start, end in merged
            ],
        }

    def _own_free_busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        targets = self._event_calendars(None)
        intervals: list[tuple[datetime, datetime]] = []
        for name, target_calendar in targets:
            try:
                # `_event_calendars(None)` just freshly listed everything, so
                # (like list_events's all-calendars branch) query the object
                # directly rather than going through the cache-retry path.
                objs = target_calendar.search(start=start, end=end, event=True, expand=False)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise CalendarNotFoundError(f"Calendar '{name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc

            for obj in objs:
                interval = event_mapping.event_busy_interval(obj.icalendar_component)
                if interval is not None:
                    intervals.append(interval)

        return event_mapping.merge_busy_intervals(intervals)

    def _free_busy_for_user(
        self, benutzer: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        principal = self._get_principal()
        is_mailto = benutzer.strip().lower().startswith("mailto:")
        address = benutzer if is_mailto else f"mailto:{benutzer}"
        bare = address[len("mailto:") :]

        # RFC 5545 3.6.4 (and RFC 6638's scheduling profile of it): a
        # VFREEBUSY's DTSTART/DTEND are UTC. caldav puts these datetimes
        # straight into the VFREEBUSY it POSTs, where `icalendar` would write a
        # zone-aware bound as `DTSTART;TZID=Europe/Berlin:...` - a TZID
        # reference in a request that carries no VTIMEZONE to resolve it with.
        try:
            response = principal.freebusy_request(
                start.astimezone(timezone.utc), end.astimezone(timezone.utc), [address]
            )
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

        if not isinstance(response, dict):
            raise TaskMcpError(
                f"Nextcloud returned an unexpected free/busy response for '{benutzer}'."
            )

        errors = response.get("errors") or {}
        entry = response.get(address, response.get(bare))
        if entry is None:
            # A single-attendee request should get back at most one non-error
            # key; if the server echoed the recipient back in some other form
            # (case, trailing slash, ...) this still finds it without having
            # to guess every possible normalization.
            other_keys = [key for key in response if key != "errors"]
            if len(other_keys) == 1:
                entry = response[other_keys[0]]

        if entry is None:
            detail = errors.get(address) or errors.get(bare) or next(iter(errors.values()), None)
            message = (
                f"Nextcloud could not provide free/busy information for '{benutzer}' "
                "(the user may be unknown, or scheduling may be disabled on the server)."
            )
            if detail:
                message += f" Server status: {detail}"
            raise TaskMcpError(message)

        try:
            component = entry.icalendar_component
        except Exception as exc:
            raise _translate(exc) from exc

        periods = event_mapping.extract_freebusy_periods(component)
        return event_mapping.merge_busy_intervals(periods)

    # ------------------------------------------------------------------
    # Calendar sharing (Nextcloud DAV extension - not part of any CalDAV
    # RFC, so this only works against a real Nextcloud server)
    # ------------------------------------------------------------------

    def share_calendar(
        self,
        kalender_name: str,
        empfaenger: str,
        gruppe: bool = False,
        schreibzugriff: bool = False,
    ) -> dict[str, Any]:
        """Share a task list or event calendar with a Nextcloud user or group.

        Uses Nextcloud's own CalDAV sharing extension (POST `{DAV:}share` to
        the collection's own URL) - this has no equivalent in any CalDAV RFC,
        so it only works against a real Nextcloud server. Calling this again
        for the same `empfaenger` updates their access level rather than
        creating a duplicate share.
        """
        if not empfaenger or not empfaenger.strip():
            raise TaskMcpError("empfaenger is required to share a calendar.")

        with self._lock:
            calendar = self._resolve_collection_any(kalender_name)
            body = _share_request_body(
                _principal_href(empfaenger, gruppe), remove=False, read_write=schreibzugriff
            )
            response = self._dav_request(
                calendar.url,
                "POST",
                body,
                {"Content-Type": "application/xml; charset=utf-8"},
                forbidden_message=(
                    f"Nextcloud denied sharing '{kalender_name}' with '{empfaenger}' "
                    "(permission denied, or the sharing backend is disabled)."
                ),
            )
            self._raise_for_share_response(response, kalender_name, empfaenger)

        return {
            "kalender_name": kalender_name,
            "empfaenger": empfaenger,
            "schreibzugriff": schreibzugriff,
        }

    def unshare_calendar(self, kalender_name: str, empfaenger: str, gruppe: bool = False) -> None:
        """Remove a user's or group's share of a task list or event calendar.

        A no-op (not an error) if `empfaenger` doesn't currently have a share
        of this calendar - Nextcloud's sharing plugin doesn't distinguish
        "removed" from "wasn't shared" in its response.
        """
        if not empfaenger or not empfaenger.strip():
            raise TaskMcpError("empfaenger is required to unshare a calendar.")

        with self._lock:
            calendar = self._resolve_collection_any(kalender_name)
            body = _share_request_body(
                _principal_href(empfaenger, gruppe), remove=True, read_write=False
            )
            response = self._dav_request(
                calendar.url,
                "POST",
                body,
                {"Content-Type": "application/xml; charset=utf-8"},
                forbidden_message=(
                    f"Nextcloud denied unsharing '{kalender_name}' from '{empfaenger}' "
                    "(permission denied, or the sharing backend is disabled)."
                ),
            )
            self._raise_for_share_response(response, kalender_name, empfaenger)

    def _raise_for_share_response(self, response: Any, kalender_name: str, empfaenger: str) -> None:
        if response.status in (200, 204, 207):
            return
        if response.status == 404:
            raise TaskMcpError(
                f"Nextcloud could not find user/group '{empfaenger}' to share "
                f"'{kalender_name}' with."
            )
        if response.status == 400:
            raise TaskMcpError(
                f"Nextcloud rejected the sharing request for '{kalender_name}' as invalid "
                f"(check that '{empfaenger}' is a valid user/group id)."
            )
        logger.warning(
            "Unexpected sharing response %s for calendar %s", response.status, kalender_name
        )
        raise TaskMcpError(
            f"Nextcloud rejected the sharing request for '{kalender_name}' "
            f"(HTTP {response.status})."
        )

    def list_calendar_shares(self, kalender_name: str) -> list[dict[str, Any]]:
        """List everyone a task list or event calendar is currently shared with.

        Reads Nextcloud's `{oc}invite` DAV property (PROPFIND, Depth 0) -
        like `share_calendar`, this only works against a real Nextcloud
        server.
        """
        with self._lock:
            calendar = self._resolve_collection_any(kalender_name)
            response = self._dav_request(
                calendar.url,
                "PROPFIND",
                _invite_propfind_body(),
                {"Content-Type": "application/xml; charset=utf-8", "Depth": "0"},
                forbidden_message=(
                    f"Nextcloud denied reading the shares of '{kalender_name}' (permission denied)."
                ),
            )
            if response.status not in (200, 207):
                logger.warning(
                    "Unexpected share-listing response %s for calendar %s",
                    response.status,
                    kalender_name,
                )
                raise TaskMcpError(
                    f"Nextcloud returned an unexpected error listing the shares of "
                    f"'{kalender_name}' (HTTP {response.status})."
                )
            return _parse_invite_response(response.tree)

    # ------------------------------------------------------------------
    # Trash bin (Nextcloud calendar-trashbin DAV plugin)
    # ------------------------------------------------------------------

    def list_trash(self) -> list[dict[str, Any]]:
        """List deleted calendar objects (tasks/events) in Nextcloud's trash bin.

        Reads Nextcloud's calendar-trashbin plugin via a `calendar-query`
        REPORT on `.../trashbin/objects/` (see `_trashbin_report_body` for
        why not PROPFIND) - a non-Nextcloud CalDAV server has no such
        collection, which is reported as a clean "not available" error
        rather than a raw 404/405.
        """
        with self._lock:
            response = self._dav_request(
                self._trashbin_objects_url(),
                "REPORT",
                _trashbin_report_body(),
                {"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
                forbidden_message="Nextcloud denied access to the trash bin (permission denied).",
            )
            if response.status in (404, 405):
                raise TaskMcpError("The trash bin is not available on this server.")
            if response.status not in (200, 207):
                logger.warning("Unexpected trashbin listing response %s", response.status)
                raise TaskMcpError(
                    f"Nextcloud returned an unexpected error listing the trash bin "
                    f"(HTTP {response.status})."
                )

            items: list[dict[str, Any]] = []
            for href, props in _iter_multistatus_responses(response.tree):
                name = unquote(href.rstrip("/").rsplit("/", 1)[-1])
                if not _TRASH_ID_RE.match(name):
                    # The `objects/` collection's own Depth-1 response entry,
                    # or anything else that isn't a trashed calendar object.
                    continue

                deleted_el = props.get(_clark(_NC_NS, "deleted-at"))
                calendar_uri_el = props.get(_clark(_NC_NS, "calendar-uri"))
                calendar_data_el = props.get(_clark(_CALDAV_NS, "calendar-data"))
                displayname_el = props.get(_clark(_DAV_NS, "displayname"))

                titel, typ = _derive_title_and_type(
                    calendar_data_el.text if calendar_data_el is not None else None
                )
                if titel is None and displayname_el is not None and displayname_el.text:
                    titel = displayname_el.text.strip()

                items.append(
                    {
                        "id": name,
                        "titel": titel,
                        "typ": typ,
                        "kalender": (
                            calendar_uri_el.text.strip()
                            if calendar_uri_el is not None and calendar_uri_el.text
                            else None
                        ),
                        "geloescht_am": _parse_deleted_at(
                            deleted_el.text if deleted_el is not None else None
                        ),
                    }
                )
            return items

    def restore_from_trash(self, trash_id: str) -> None:
        """Restore a deleted calendar object from the trash bin to its original calendar.

        MOVEs the trashbin entry from `.../trashbin/objects/<trash_id>` to
        `.../trashbin/restore/<trash_id>`; Nextcloud's server restores it to
        its original calendar as a side effect of that move, not this method.
        """
        if not trash_id or not trash_id.strip():
            raise TaskMcpError("id is required to restore an item from the trash bin.")

        with self._lock:
            source = self._trashbin_objects_url().join(trash_id)
            destination = self._trashbin_restore_url().join(trash_id)
            response = self._dav_request(
                source,
                "MOVE",
                "",
                {"Destination": str(destination)},
                forbidden_message=(
                    f"Nextcloud denied restoring trash item '{trash_id}' (permission denied)."
                ),
            )
            if response.status == 404:
                raise TaskMcpError(f"Trash item '{trash_id}' was not found in the trash bin.")
            if response.status == 405:
                raise TaskMcpError("The trash bin is not available on this server.")
            if response.status not in (200, 201, 204):
                logger.warning(
                    "Unexpected trashbin restore response %s for %s", response.status, trash_id
                )
                raise TaskMcpError(
                    f"Nextcloud rejected restoring trash item '{trash_id}' "
                    f"(HTTP {response.status})."
                )

    # ------------------------------------------------------------------
    # ICS import / export
    # ------------------------------------------------------------------

    def export_calendar(self, kalender_name: str) -> dict[str, str]:
        """Export a task list or event calendar as a single ICS (VCALENDAR) text.

        Built client-side for portability: every object in the collection
        (`calendar.events()` / `calendar.todos()`, whichever the collection
        supports) is fetched, and its components (including any VTIMEZONEs
        and, for a recurring event/task, its override instances - these
        already live together in one calendar object) are merged into one
        VCALENDAR with a single PRODID/VERSION header. VTIMEZONE components
        are de-duplicated by TZID.
        """
        with self._lock:
            calendar = self._resolve_collection_any(kalender_name)

            try:
                events = calendar.events() if self._supports_component(calendar, "VEVENT") else []
                todos = (
                    calendar.todos(include_completed=True)
                    if self._supports_component(calendar, "VTODO")
                    else []
                )
            except caldav_error.NotFoundError as exc:
                raise TaskMcpError(
                    f"Calendar or task list '{kalender_name}' was not found."
                ) from exc
            except Exception as exc:
                raise _translate(exc) from exc

            merged = Calendar()
            merged.add("prodid", "-//nextcloud-task-mcp//EN")
            merged.add("version", "2.0")
            seen_tzids: set[str] = set()
            for obj in list(events) + list(todos):
                try:
                    instance = obj.icalendar_instance
                except Exception as exc:
                    raise _translate(exc) from exc
                for component in instance.subcomponents:
                    if component.name == "VTIMEZONE":
                        tzid = str(component.get("TZID", ""))
                        if tzid:
                            if tzid in seen_tzids:
                                continue
                            seen_tzids.add(tzid)
                    merged.add_component(component)

            return {"kalender_name": kalender_name, "ics": merged.to_ical().decode("utf-8")}

    def import_ics(self, kalender_name: str, ics: str) -> dict[str, Any]:
        """Import ICS text into a task list or event calendar.

        Top-level VEVENT/VTODO components are grouped by UID (so a recurring
        event/task and its override instances stay together as ONE saved
        calendar object, along with any VTIMEZONEs from the source ICS), then
        each group is saved via `calendar.save_event`/`calendar.save_todo`.
        A group whose kind (VEVENT/VTODO) the target collection doesn't
        support is skipped rather than failing the whole import.
        """
        if not ics or not ics.strip():
            raise InvalidIcsDataError("ics is required and must not be empty.")
        try:
            parsed = Calendar.from_ical(ics)
        except Exception as exc:
            raise InvalidIcsDataError(f"Could not parse ics: {exc}") from exc
        if getattr(parsed, "name", None) != "VCALENDAR":
            raise InvalidIcsDataError("ics must be a VCALENDAR.")

        timezones = [c for c in parsed.subcomponents if c.name == "VTIMEZONE"]
        groups: dict[tuple[str, str], list[Any]] = {}
        for component in parsed.subcomponents:
            if component.name not in ("VEVENT", "VTODO"):
                continue
            uid = str(component.get("UID") or uuid.uuid4())
            groups.setdefault((uid, component.name), []).append(component)

        if not groups:
            raise InvalidIcsDataError("ics must contain at least one VEVENT or VTODO component.")

        with self._lock:
            calendar = self._resolve_collection_any(kalender_name)

            importiert = 0
            uebersprungen = 0
            for (_uid, kind), components in groups.items():
                if not self._supports_component(calendar, kind):
                    uebersprungen += 1
                    continue

                sub_calendar = Calendar()
                sub_calendar.add("prodid", "-//nextcloud-task-mcp//EN")
                sub_calendar.add("version", "2.0")
                for tz in timezones:
                    sub_calendar.add_component(tz)
                for component in components:
                    sub_calendar.add_component(component)
                ical_text = sub_calendar.to_ical().decode("utf-8")

                try:
                    if kind == "VEVENT":
                        calendar.save_event(ical=ical_text)
                    else:
                        calendar.save_todo(ical=ical_text)
                except TaskMcpError:
                    raise
                except Exception as exc:
                    raise _translate(exc) from exc
                importiert += 1

        return {
            "kalender_name": kalender_name,
            "importiert": importiert,
            "uebersprungen": uebersprungen,
        }
