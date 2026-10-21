"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a *host integrator* relies on when they read the package
surface rather than the source. The behavioural suites live alongside.

The bug that motivated them: ``asas_workflow`` re-exported names from a
submodule called ``seed``, so ``asas_workflow.seed`` resolved to the **module**,
not a callable. The README documented ``seed(session)`` as part of the host
contract, so following the documentation raised ``TypeError: 'module' object is
not callable``. Nothing caught it, because every package suite tested the
package against its own assumptions rather than against the published contract.
"""

import types

import asas_uaepass

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
# opposed to ones bound as a side effect of ``from .x import y``. The
# nationality list is part of the API: a host's own "declared nationality"
# picker reads the same codes the parser writes.
INTENTIONAL_MODULE_EXPORTS = {"nationalities"}


def test_declares_all():
    assert hasattr(asas_uaepass, "__all__"), "asas_uaepass must declare __all__"
    assert asas_uaepass.__all__, "asas_uaepass.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_uaepass.__all__ if not hasattr(asas_uaepass, n)]
    assert not missing, f"asas_uaepass.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_uaepass, n) and not callable(getattr(asas_uaepass, n))
    ]
    assert not broken, (
        f"asas_uaepass exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function; rename the submodule."
    )


def test_this_package_exposes_no_host_hooks():
    """Everything is an object the host constructs and owns, the asas-graph
    shape, so none of the contract hooks exist here."""
    assert not any(hasattr(asas_uaepass, n) for n in CONTRACT_CALLABLES)


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_uaepass.__all__
        if isinstance(getattr(asas_uaepass, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_uaepass exports undeclared modules: {sorted(unexpected)}. If that is "
        f"deliberate, add them to INTENTIONAL_MODULE_EXPORTS; if not, the name is "
        f"shadowing something."
    )


def test_version_is_exported():
    assert isinstance(asas_uaepass.__version__, str)
    assert asas_uaepass.__version__
