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

import asas_cv

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

# Submodules this package re-exports **on purpose** (``from . import x``): the
# three pure pieces, each usable whole.
INTENTIONAL_MODULE_EXPORTS = {"photo", "resume", "text"}


def test_declares_all():
    assert hasattr(asas_cv, "__all__"), "asas_cv must declare __all__"
    assert asas_cv.__all__, "asas_cv.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_cv.__all__ if not hasattr(asas_cv, n)]
    assert not missing, f"asas_cv.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [n for n in CONTRACT_CALLABLES if hasattr(asas_cv, n) and not callable(getattr(asas_cv, n))]
    assert not broken, (
        f"asas_cv exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function; rename the submodule."
    )


def test_this_package_exposes_no_host_hooks():
    """Three pure functions and a schema: none of the contract hooks exist."""
    assert not any(hasattr(asas_cv, n) for n in CONTRACT_CALLABLES)


def test_module_exports_are_intentional():
    exported = {n for n in asas_cv.__all__ if isinstance(getattr(asas_cv, n), types.ModuleType)}
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_cv exports undeclared modules: {sorted(unexpected)}. If that is "
        f"deliberate, add them to INTENTIONAL_MODULE_EXPORTS; if not, the name is "
        f"shadowing something."
    )
    assert exported == INTENTIONAL_MODULE_EXPORTS


def test_version_is_exported():
    assert isinstance(asas_cv.__version__, str)
    assert asas_cv.__version__
