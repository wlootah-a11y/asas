"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a *host integrator* relies on when they read the package
surface rather than the source. The behavioural suites live alongside.

The bug that motivated them: ``asas_workflow`` re-exported names from a
submodule called ``seed``, so ``asas_workflow.seed`` resolved to the **module**,
not a callable. The README documented ``seed(session)`` as part of the host
contract, so following the documentation raised ``TypeError: 'module' object is
not callable``. Nothing caught it, because every package suite tested the
package against its own assumptions rather than against the published contract.

This package adds one invariant of its own: ``import asas_mail`` must not
import ``asas_graph`` (and through it httpx and msal), which is the ``graph``
extra resolved on first use.
"""

import subprocess
import sys
import types

import asas_mail

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
    assert hasattr(asas_mail, "__all__"), "asas_mail must declare __all__"
    assert asas_mail.__all__, "asas_mail.__all__ must not be empty"


def test_every_exported_name_resolves():
    """The lazily resolved names count: hasattr runs the module ``__getattr__``."""
    missing = [n for n in asas_mail.__all__ if not hasattr(asas_mail, n)]
    assert not missing, f"asas_mail.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_mail, n) and not callable(getattr(asas_mail, n))
    ]
    assert not broken, (
        f"asas_mail exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function; rename the submodule."
    )


def test_configure_is_the_one_host_hook():
    """Table-less and router-less: ``configure`` installs the mailer factory
    and nothing else from the contract exists."""
    assert callable(asas_mail.configure)
    assert not any(hasattr(asas_mail, n) for n in CONTRACT_CALLABLES if n != "configure")


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_mail.__all__
        if isinstance(getattr(asas_mail, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_mail exports undeclared modules: {sorted(unexpected)}. If that is "
        f"deliberate, add them to INTENTIONAL_MODULE_EXPORTS; if not, the name is "
        f"shadowing something."
    )


def test_version_is_exported():
    assert isinstance(asas_mail.__version__, str)
    assert asas_mail.__version__


def test_importing_the_package_loads_no_graph_client():
    """Checked in a fresh interpreter, because this test process has already
    imported asas_graph."""
    code = (
        "import sys, asas_mail\n"
        "assert 'asas_graph' not in sys.modules, 'asas_graph imported'\n"
        "assert 'httpx' not in sys.modules, 'httpx imported'\n"
        "asas_mail.GraphTransport\n"
        "assert 'asas_graph' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_an_unknown_attribute_is_still_an_attribute_error():
    try:
        asas_mail.no_such_name
    except AttributeError as exc:
        assert "no_such_name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError")
