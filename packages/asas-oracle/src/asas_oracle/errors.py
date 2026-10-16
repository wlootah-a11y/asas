"""The error hierarchy. None of these carries an HTTP status for the HOST's
callers: how an Oracle failure is reported onward is host policy.

What they never carry either is Oracle's response BODY. It holds tenant detail,
and an attachment listing holds signed download URLs, so the text is logged at
debug level by the client and kept off the exception entirely. The status, the
verb and the path are enough to act on.
"""

from __future__ import annotations


class OracleError(Exception):
    """Base class for everything this package raises."""


class OracleConfigError(OracleError):
    """The host wired the settings wrong (raised at construction)."""


class OracleQueryError(OracleError, ValueError):
    """A value cannot be written into Oracle's ``q`` grammar safely.

    The grammar has no escape form: a single quote ends the literal and a
    ``;`` starts another clause, so a value holding either would change what
    the query means (an input that reads more rows than the caller asked for).
    Raised BEFORE any request, so nothing is sent."""


class OracleNotConfiguredError(OracleError):
    """No base URL, so the integration is off for this deployment.

    Raised on the first call rather than at construction, because "Oracle is
    not configured here" is a legitimate state for a host to boot in: a status
    page can ask ``client.configured`` and answer instantly."""


class OracleUpstreamError(OracleError):
    """Oracle refused, timed out, or answered something that is not JSON.

    ``status`` is Oracle's HTTP status, or ``None`` when no response arrived
    (a transport failure or a malformed body)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str = "",
        path: str = "",
        retry_after_seconds: int | None = None,
        body: str = "",
        content_type: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        #: What the upstream asked a caller to wait (``Retry-After`` on a 429 or
        #: a 503), or ``None`` when it said nothing.
        self.retry_after_seconds = retry_after_seconds
        #: The refusal's first characters and its media type, FOR
        #: CLASSIFICATION ONLY.
        #:
        #: A host often has to tell refusals apart more finely than this
        #: package can: which of its own error codes a 404 becomes, whether a
        #: 405 through a gateway means an unregistered operation, whether an
        #: HTML error page means the base URL is wrong rather than the record
        #: missing. All of that is read off the body, and without it here a
        #: host has to drop below the client and lose the cache and the
        #: breaker with it.
        #:
        #: NEVER SHOW IT. Oracle's bodies carry tenant detail, and its
        #: attachment documents carry signed download URLs, so this is for
        #: branching in code and never for a message, a log line or an API
        #: response. Truncated for that reason as much as for size.
        self.body = body
        self.content_type = content_type

    @property
    def is_transient(self) -> bool:
        """Worth retrying later: no answer at all, a throttle, or a 5xx."""
        return self.status is None or self.status == 429 or self.status >= 500


class OracleAuthError(OracleUpstreamError):
    """Oracle or the gateway refused the credentials (401) or the permission
    (403), or a token could not be obtained.

    Its own class because it is a CONFIGURATION fault, not an outage: retrying
    will not fix it, it never trips the circuit breaker, and it says to look at
    the key, the account or the role rather than at the network."""

    @property
    def is_transient(self) -> bool:
        return False


class OracleNotFoundError(OracleUpstreamError):
    """Oracle answered 404: a real answer about a real record, not a fault.
    A retry will not bring the record back."""


class OracleAlreadyExistsError(OracleUpstreamError):
    """Oracle refused a create because a key the CALLER minted is taken.

    Its own class because it is the one refusal that can mean success: when the
    caller chooses the key (a record number it mints itself) and retries a create,
    this answer says the first attempt landed. The caller reads the record back
    by that key. On a first attempt it means a genuine collision."""


class OracleLockedError(OracleUpstreamError):
    """Another user holds the record's lock in Fusion.

    A 400 with prose, not a 409 and not a 423, so it is told apart by its
    words like the duplicate refusal is. TRANSIENT and therefore its own
    class: somebody has the record open in the Fusion UI and the same write
    lands once they let go, so a caller with an outbox should BACK OFF and
    retry rather than park the row as failed.
    """


class OracleStateRefusedError(OracleUpstreamError):
    """Fusion's own lifecycle refused a phase or state change on a record.

    A 400 with prose, like the duplicate and the lock, so it is told apart by
    its words rather than by its status. Its own class because it is a VERDICT
    ON THE RECORD and not a fault: the record is not in a state the move can
    start from, the target is not adjacent to the current one, or the record's
    own lifecycle row disagrees with its state. Sending the same move again
    gets the same answer, so a caller with an outbox must FAIL the row rather
    than spend its attempts, which is the opposite of what
    :class:`OracleLockedError` asks for.

    Measured on a Fusion recruiting instance across 169 phase and state moves.
    Five distinct refusals share this shape, all emitted by
    ``LifecycleAMImpl.setCurrentPhaseAndState`` or
    ``PhaseStepProcessor.activateStateStep``.

    Not transient, so it never trips the circuit breaker: one record refusing
    a move says nothing about whether Oracle is reachable.
    """

    @property
    def is_transient(self) -> bool:
        return False


class OraclePhaseEntryRequiredError(OracleStateRefusedError):
    """The ONE lifecycle refusal a second attempt can get past.

    Fusion refuses a move that lands a TERMINAL state in a phase the record is
    not in yet ("Cannot move directly into new phase terminal state"). Entering
    that phase at a non-terminal state first and asking again is accepted.

    It is a subclass, so a caller that only knows the parent still treats it as
    final, which is the safe default. A caller that knows this class can walk
    the lifecycle. It is deliberately NOT raised for any other refusal: "target
    state is not adjacent" is not helped by an extra hop, and attempting one
    could leave the record in a state nobody asked for.

    Measured narrower than it first looks. On the instance tested, only the
    CANCELED state is refused across a phase boundary; the equivalent moves
    into Filled and Rejected were accepted, so a caller must not assume every
    terminal state behaves this way.
    """


class OracleGatewayError(OracleUpstreamError):
    """The API gateway refused before Oracle was reached at all.

    An operation it does not carry, a transport it was not registered for, a
    path parameter it cannot map. Worth its own class because the fix is in a
    different place and belongs to different people: a gateway registration,
    not an Oracle record, a credential or this code.
    """


class OracleUnavailableError(OracleUpstreamError):
    """The client's circuit breaker is open: this process saw Oracle fail
    several times in a row and is not asking again until the cooldown ends.

    Raised at once, before any request, so a caller learns of the outage in
    microseconds rather than after a timeout. A subclass of the upstream error,
    so a fail-soft caller already handles it; ``retry_after_seconds`` says when
    the breaker will let one probe through. Transient by definition."""

    def __init__(self, message: str, *, retry_after_seconds: int, method: str = "", path: str = "") -> None:
        super().__init__(
            message, status=None, method=method, path=path, retry_after_seconds=retry_after_seconds
        )


def transport_detail(exc: BaseException) -> str:
    """What a transport failure WAS, in one line: the error's type and its own
    words, or its cause's.

    httpx's connect and timeout errors often carry no message at all, which
    leaves a log reading "transport failure on GET /x:" with nothing after it
    and no way to tell a refused connection from a DNS failure, a TLS fault or
    a timeout. The TYPE is the part that always says something.
    """
    words = str(exc).strip() or str(exc.__cause__ or "").strip() or "no message"
    return f"{type(exc).__name__}: {words}"
