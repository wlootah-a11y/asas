"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a *host integrator* relies on when they read the package
surface rather than the source. The behavioural suites live alongside.

The bug that motivated them: ``asas_workflow`` re-exported names from a
submodule called ``seed``, so ``asas_workflow.seed`` resolved to the **module**,
not a callable. The README documented ``seed(session)`` as part of the host
contract, so following the documentation raised ``TypeError: 'module' object is
not callable``. Nothing caught it, because every package suite tested the
package against its own assumptions rather than against the published contract.

This package adds one invariant of its own: ``import asas_servicetoken`` must
not import httpx or fastapi, which are extras resolved on first use.
"""

import subprocess
import sys
import types

import asas_servicetoken

# Names the host contract documents as callable. A package exposes some subset;
# whichever it exposes must actually be callable, never a shadowing submodule.
CONTRACT_CALLABLES = (
    "build_routers",
    "build_router",
    "build_mcp_app",
    "seed",
    "migrate",
    "configure",
)

# Submodules this package re-exports **on purpose** (``from . import x``), as
# opposed to ones bound as a side effect of ``from .x import y``. Keeping the
# list explicit is the point: a new entry has to be justified in review.
INTENTIONAL_MODULE_EXPORTS = set()


def test_declares_all():
    """``__all__`` is the package's statement of its own public surface. Without
    one, ``import *`` leaks every transitively-imported name and a reader cannot
    tell the contract from an implementation detail."""
    assert hasattr(asas_servicetoken, "__all__"), "asas_servicetoken must declare __all__"
    assert asas_servicetoken.__all__, "asas_servicetoken.__all__ must not be empty"


def test_every_exported_name_resolves():
    """A name in ``__all__`` that does not exist is a typo that surfaces only on
    ``import *``, or never. The lazily resolved names count: hasattr runs the
    module ``__getattr__``."""
    missing = [n for n in asas_servicetoken.__all__ if not hasattr(asas_servicetoken, n)]
    assert not missing, f"asas_servicetoken.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    """The seed trap, pinned. Whichever contract names this package exposes must
    be callable; a submodule shadowing one is invisible until someone calls it."""
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_servicetoken, n) and not callable(getattr(asas_servicetoken, n))
    ]
    assert not broken, (
        f"asas_servicetoken exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function; rename the submodule."
    )


def test_this_package_exposes_no_host_hooks():
    """Table-less and router-less: a host builds an Issuer and a Verifier and
    calls nothing else, so none of the contract hooks exist here."""
    assert not any(hasattr(asas_servicetoken, n) for n in CONTRACT_CALLABLES)


def test_module_exports_are_intentional():
    """Modules may be exported (``asas_search.fts`` is part of its API), but only
    deliberately. An unlisted one usually means a submodule leaked into __all__."""
    exported = {
        n for n in asas_servicetoken.__all__
        if isinstance(getattr(asas_servicetoken, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_servicetoken exports undeclared modules: {sorted(unexpected)}. If that is "
        f"deliberate, add them to INTENTIONAL_MODULE_EXPORTS; if not, the name is "
        f"shadowing something."
    )


def test_version_is_exported():
    """Hosts pin by version; the running code must be able to say what it is."""
    assert isinstance(asas_servicetoken.__version__, str)
    assert asas_servicetoken.__version__


def test_importing_the_package_loads_neither_extra():
    """httpx and fastapi are extras resolved on first use, so a host that wants
    neither pays for neither. Checked in a fresh interpreter, because this
    test process has already imported both."""
    code = (
        "import sys, asas_servicetoken\n"
        "assert 'httpx' not in sys.modules, 'httpx imported'\n"
        "assert 'fastapi' not in sys.modules, 'fastapi imported'\n"
        "asas_servicetoken.send_with_service_token\n"
        "assert 'httpx' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_an_unknown_attribute_is_still_an_attribute_error():
    try:
        asas_servicetoken.no_such_name
    except AttributeError as exc:
        assert "no_such_name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError")
