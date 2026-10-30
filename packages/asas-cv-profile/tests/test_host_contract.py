"""Host-contract conformance (Teamy TEAMY-798), and the import a host pays for: the
package loads neither the Agents SDK, a model client nor the matching libraries until a
name that needs them is used."""

import subprocess
import sys
import types
from pathlib import Path

import asas_cv_profile

SRC = str(Path(__file__).resolve().parents[1] / "src")
CONTRACT_CALLABLES = ("build_routers", "build_router", "build_mcp_app", "seed", "migrate", "configure")


def test_declares_all_and_every_name_resolves():
    assert asas_cv_profile.__all__
    missing = [n for n in asas_cv_profile.__all__ if not hasattr(asas_cv_profile, n)]
    assert not missing, f"asas_cv_profile.__all__ names that do not resolve: {missing}"


def test_exports_no_module():
    modules = [n for n in asas_cv_profile.__all__ if isinstance(getattr(asas_cv_profile, n), types.ModuleType)]
    assert not modules, modules


def test_owns_no_tables_and_claims_no_contract_hook():
    """Table-less: no schema, no seed, no configure. The upload page is a router of its
    own shape (a builder and a folder, no session), so it does not take the contract's name."""
    assert not any(hasattr(asas_cv_profile, n) for n in CONTRACT_CALLABLES)
    assert callable(asas_cv_profile.upload_router)


def test_version_is_exported():
    assert isinstance(asas_cv_profile.__version__, str) and asas_cv_profile.__version__


def test_import_is_light():
    """Checked in a fresh interpreter, so this suite's own imports cannot mask a regression."""
    code = ("import sys; import asas_cv_profile; "
            "heavy = [m for m in ('agents', 'openai', 'camel_tools', 'rapidfuzz', 'dateparser', 'fastapi', 'azure') if m in sys.modules]; "
            "print(','.join(heavy))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, env={"PYTHONPATH": SRC})
    assert out.stdout.strip() == "", out.stdout
