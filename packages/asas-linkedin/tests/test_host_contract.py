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

import asas_linkedin

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
# opposed to ones bound as a side effect of ``from .x import y``. Each is a
# pure, self-contained piece a host may want whole: the Apply with LinkedIn
# check, the feed writer, the little-text escaper, the OIDC verifier (usable
# for any RS256 provider), the post body, and the sealing.
INTENTIONAL_MODULE_EXPORTS = {"awli", "jobs_feed", "little_text", "oidc", "posts", "sealing"}


def test_declares_all():
    assert hasattr(asas_linkedin, "__all__"), "asas_linkedin must declare __all__"
    assert asas_linkedin.__all__, "asas_linkedin.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_linkedin.__all__ if not hasattr(asas_linkedin, n)]
    assert not missing, f"asas_linkedin.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_linkedin, n) and not callable(getattr(asas_linkedin, n))
    ]
    assert not broken, (
        f"asas_linkedin exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function; rename the submodule."
    )


def test_this_package_exposes_no_host_hooks():
    """Everything is an object the host constructs and owns, the asas-graph
    shape, so none of the contract hooks exist here."""
    assert not any(hasattr(asas_linkedin, n) for n in CONTRACT_CALLABLES)


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_linkedin.__all__
        if isinstance(getattr(asas_linkedin, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_linkedin exports undeclared modules: {sorted(unexpected)}. If that is "
        f"deliberate, add them to INTENTIONAL_MODULE_EXPORTS; if not, the name is "
        f"shadowing something."
    )
    assert exported == INTENTIONAL_MODULE_EXPORTS


def test_version_is_exported():
    assert isinstance(asas_linkedin.__version__, str)
    assert asas_linkedin.__version__
