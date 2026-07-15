"""Thin, connection-reusing wrapper around the caldav library for VTODO management."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, time, timedelta, timezone
from typing import Any, TypeVar
from urllib.parse import unquote

from caldav.collection import Calendar as DAVCalendar
from caldav.collection import Principal as DAVPrincipal
from caldav.davclient import DAVClient
from caldav.elements import dav
from caldav.elements import ical as ical_elements
from caldav.lib import error as caldav_error
from icalendar import Calendar, Event, Todo
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
    InvalidTaskDataError,
    TaskConflictError,
    TaskListAlreadyExistsError,
    TaskListNotFoundError,
    TaskMcpError,
    TaskNotFoundError,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

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

# The two supported task<->event link semantics, mapped to the RELATED-TO
# RELTYPE written on the *event* (never on the task - a RELATED-TO added to a
# VTODO would make Nextcloud Tasks render the task as a subtask of a
# non-task, garbling its UI; the event side has no such interpretation):
#   "zeitblock":      the event reserves time for the task (event = child).
#   "voraussetzung":  the event must happen before the task (event = parent).
_LINK_RELTYPES: dict[str, str] = {"zeitblock": "PARENT", "voraussetzung": "CHILD"}

# Runs of anything that isn't an ASCII letter/digit collapse to a single
# hyphen, so "Groceries & Errands!" -> "groceries-errands" (leading/trailing
# hyphens stripped separately, below).
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


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


def _trashbin_propfind_body() -> str:
    """Build a PROPFIND body for listing trashbin objects (Nextcloud's
    calendar-trashbin plugin, namespace `http://nextcloud.com/ns`)."""
    root = etree.Element(
        _clark(_DAV_NS, "propfind"), nsmap={"d": _DAV_NS, "c": _CALDAV_NS, "nc": _NC_NS}
    )
    prop = etree.SubElement(root, _clark(_DAV_NS, "prop"))
    etree.SubElement(prop, _clark(_DAV_NS, "displayname"))
    etree.SubElement(prop, _clark(_CALDAV_NS, "calendar-data"))
    etree.SubElement(prop, _clark(_NC_NS, "deleted-at"))
    etree.SubElement(prop, _clark(_NC_NS, "calendar-uri"))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode("utf-8")


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
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(int(text), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


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


class CalDavService:
    """Holds one reused CalDAV connection and exposes task CRUD operations on it."""

    def __init__(self, url: str, username: str, password: str, timeout: int = 30) -> None:
        self._client = DAVClient(
            url=url,
            username=username,
            password=password,
            timeout=timeout,
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
        self._calendar_cache: dict[tuple[str, str], DAVCalendar] = {}
        # Lazily discovered and cached like the calendar cache above (A3's
        # reasoning applies equally here): the caller's own address(es) don't
        # change during the lifetime of one CalDavService, so there is no
        # reason to re-run the principal PROPFIND(s) on every create_event
        # call that adds attendees. Guarded by `_lock`.
        self._own_organizer_address: str | None = None
        self._own_calendar_user_addresses: list[str] | None = None

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

    @staticmethod
    def _supports_component(calendar: DAVCalendar, component: str) -> bool:
        """True if `calendar` supports `component` ("VTODO"/"VEVENT"), or can't tell.

        Nextcloud advertises `supported-calendar-component-set` on every
        calendar, but a collection that doesn't (or whose PROPFIND fails,
        e.g. an external webcal subscription with flaky props) is treated as
        supporting everything - failing open here only means a name shows up
        in one listing too many, while failing closed would make an entire
        calendar silently unreachable.
        """
        try:
            components = calendar.get_supported_components()
        except Exception:
            return True
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
        try:
            calendars = self._get_principal().calendars()
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc

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

    def _resolve_and_cache(self, name: str, component: str) -> DAVCalendar:
        calendar = self._resolve_collection(name, component)
        self._calendar_cache[(component, name)] = calendar
        return calendar

    def _get_collection(self, name: str, component: str) -> DAVCalendar:
        cached = self._calendar_cache.get((component, name))
        if cached is not None:
            return cached
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
            try:
                calendars = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

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
                    self._calendar_cache[("VTODO", name)] = calendar

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
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

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

            self._calendar_cache[("VTODO", display_name)] = calendar
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
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

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
            self._calendar_cache[("VTODO", new_display_name)] = calendar
            return {"name": new_display_name, "url": str(calendar.url)}

    def list_tasks(
        self,
        list_name: str,
        only_open: bool = True,
        due_before: str | None = None,
        due_after: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return tasks in the given list, parsed into German task dicts.

        `due_before`/`due_after`/`limit` filter the already-parsed results via
        `mapping.filter_tasks` (C4) - see its docstring for the exact
        semantics (date-vs-datetime bound normalization, no-due-date
        exclusion, and `limit` validation).
        """
        with self._lock:

            def op(calendar: DAVCalendar):
                return calendar.todos(include_completed=not only_open)

            try:
                todos = self._with_calendar(list_name, op)
            except TaskMcpError:
                raise
            except caldav_error.NotFoundError as exc:
                raise TaskListNotFoundError(f"Task list '{list_name}' was not found.") from exc
            except Exception as exc:
                raise _translate(exc) from exc
            tasks = [mapping.parse_vtodo(todo.icalendar_component) for todo in todos]
            return mapping.filter_tasks(
                tasks, due_before=due_before, due_after=due_after, limit=limit
            )

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
        with self._lock:

            def op(calendar: DAVCalendar):
                todo_obj = calendar.get_todo_by_uid(task_uid)
                mapping.apply_task_fields(todo_obj.icalendar_component, fields)
                todo_obj.save()

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
        interpreted as UTC, matching `parse_datetime_input` everywhere else.
        """
        if value is None:
            return None
        parsed = mapping.parse_datetime_input(value)
        if isinstance(parsed, datetime):
            return parsed
        day_start = datetime.combine(parsed, time.min, tzinfo=timezone.utc)
        return day_start + timedelta(days=1) if exclusive_end else day_start

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

        try:
            calendars = self._get_principal().calendars()
        except TaskMcpError:
            raise
        except Exception as exc:
            raise _translate(exc) from exc
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
            try:
                calendars = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

            result: list[dict[str, Any]] = []
            for calendar in calendars:
                try:
                    components = list(calendar.get_supported_components())
                except Exception:
                    components = []
                if components and "VEVENT" not in components:
                    continue
                name = calendar.get_display_name() or str(calendar.url)
                # The color property is cosmetic; a calendar whose PROPFIND
                # for it fails should still be listed rather than error out.
                farbe: str | None
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
                        "komponenten": [str(c) for c in components],
                    }
                )
                if sum(1 for entry in result if entry["name"] == name) == 1:
                    self._calendar_cache[("VEVENT", name)] = calendar
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
            try:
                principal = self._get_principal()
                existing = principal.calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

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

            self._calendar_cache[("VEVENT", display_name)] = calendar
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
            try:
                existing = self._get_principal().calendars()
            except TaskMcpError:
                raise
            except Exception as exc:
                raise _translate(exc) from exc

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
            self._calendar_cache[("VEVENT", final_name)] = calendar
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
    ) -> list[dict[str, Any]]:
        """Return events across one, several, or all VEVENT calendars, sorted by start.

        `von`/`bis` bound the query server-side (CalDAV time-range REPORT), so
        recurring events that have an occurrence in the window are matched
        even when their master event started long before it. With
        `expand=True`, recurring events are additionally expanded into their
        individual occurrences within the window (requires both bounds).
        `suchtext`/`tag`/`limit` filter the parsed results client-side via
        `event_mapping.filter_events`.
        """
        start_bound = self._range_bound(von, exclusive_end=False)
        end_bound = self._range_bound(bis, exclusive_end=True)
        if expand and (start_bound is None or end_bound is None):
            raise InvalidEventDataError(
                "Expanding recurring events requires both von and bis bounds."
            )

        with self._lock:

            def op(calendar: DAVCalendar):
                if start_bound is None and end_bound is None:
                    return calendar.events()
                # caldav's search/expand path is only well-defined with
                # both ends present; widen an omitted side instead of
                # passing None through (see _RANGE_MIN/_RANGE_MAX).
                return calendar.search(
                    start=start_bound or _RANGE_MIN,
                    end=end_bound or _RANGE_MAX,
                    event=True,
                    expand=expand,
                )

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
                    parsed["kalender"] = name
                    events.append(parsed)

            return event_mapping.filter_events(events, suchtext=suchtext, tag=tag, limit=limit)

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
        dauer_minuten: int = 60,
    ) -> str:
        """Create a calendar event from an existing task (timeboxing) and link them.

        Title, notes, location and tags are copied from the task; the event
        starts at `start` (or, if omitted, the task's due date/time) and runs
        for `dauer_minuten`. A date-only start produces a one-day all-day
        event instead. The new event carries RELATED-TO;RELTYPE=PARENT with
        the task's UID (the "zeitblock" link semantics).
        """
        if dauer_minuten <= 0:
            raise InvalidEventDataError(f"dauer_minuten must be > 0, got {dauer_minuten}.")

        with self._lock:
            task = self.get_task(list_name, task_uid)

            start_spec = start if start is not None else task.get("faellig_datum")
            if start_spec is None:
                raise InvalidEventDataError(
                    "The task has no faellig_datum (due date); pass an explicit start "
                    "for the event instead."
                )

            parsed_start = mapping.parse_datetime_input(start_spec)
            if isinstance(parsed_start, datetime):
                ende = (parsed_start + timedelta(minutes=dauer_minuten)).isoformat()
                start_value = parsed_start.isoformat()
            else:
                # All-day due date -> one-day all-day event (inclusive end).
                start_value = parsed_start.isoformat()
                ende = start_value

            fields = event_mapping.EventFields(
                titel=task.get("titel") or "Aufgabe",
                start=start_value,
                ende=ende,
                beschreibung=task.get("notizen"),
                ort=task.get("ort"),
                tags=task.get("tags") or None,
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
        string; day boundaries are UTC, consistent with the naive-input-is-UTC
        rule used everywhere else in this server.
        """
        parsed = mapping.parse_datetime_input(datum)
        if isinstance(parsed, datetime):
            raise InvalidEventDataError(
                f"datum must be a date-only 'YYYY-MM-DD' string, got '{datum}'."
            )

        with self._lock:
            termine = self.list_events(
                calendar_names=calendar_names, von=datum, bis=datum, expand=True
            )

            if list_names is None:
                list_names = [entry["name"] for entry in self.list_task_lists()]
            aufgaben: list[dict[str, Any]] = []
            for name in list_names:
                tasks = self.list_tasks(name, only_open=True, due_before=datum, due_after=datum)
                for task in tasks:
                    task["liste"] = name
                    aufgaben.append(task)

            return {"datum": parsed.isoformat(), "termine": termine, "aufgaben": aufgaben}

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
            "von": start_bound.isoformat(),
            "bis": end_bound.isoformat(),
            "benutzer": benutzer,
            "belegt": [{"von": start.isoformat(), "bis": end.isoformat()} for start, end in merged],
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

        try:
            response = principal.freebusy_request(start, end, [address])
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

        Reads Nextcloud's calendar-trashbin plugin (PROPFIND, Depth 1, on
        `.../trashbin/objects/`) - a non-Nextcloud CalDAV server has no such
        collection, which is reported as a clean "not available" error
        rather than a raw 404/405.
        """
        with self._lock:
            response = self._dav_request(
                self._trashbin_objects_url(),
                "PROPFIND",
                _trashbin_propfind_body(),
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
