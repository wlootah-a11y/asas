"""The command line and the upload page, with the model scripted and no network."""

import json
from datetime import date

import asas_agent
import pytest
from conftest import FIXTURES, Scripted, docx, lines, run

from asas_cv_profile import ProfileBuilder, cli, write_outputs


def built_profile(model):
    def answer(call, s):
        if s.name == "Name":
            return {"full_name": "Sara Example", "lines": [[1, 1]]}
        return None

    text = lines("Sara Example", "EXPERIENCE", "Analyst, Example Co, 2019-2021")
    return run(ProfileBuilder(model, Scripted(model, answer)).build(b"x", "cv.txt", as_of=date(2026, 10, 28), text=text))


def test_schema_lists_the_calls(capsys):
    assert cli.main(["schema", "--profile-model", str(FIXTURES / "profile.yaml"), "--split"]) == 0
    assert "ProfileGroup1:" in capsys.readouterr().out


def test_a_bad_profile_model_exits_with_where_it_is_wrong(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("sections: [{name: A, fields: [{name: B, type: money}]}]")
    assert cli.main(["schema", "--profile-model", str(bad)]) == 2
    assert "unknown type 'money'" in capsys.readouterr().err


def test_a_model_call_needs_the_gateway_settings(tmp_path, monkeypatch):
    for key in asas_agent.GATEWAY_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        cli.main(["one", str(tmp_path / "cv.docx"), "--profile-model", str(FIXTURES / "profile.yaml"), "--out", str(tmp_path / "out")])


def test_recheck_reads_saved_results_again_without_a_model(model, tmp_path, capsys):
    folder = tmp_path / "out" / "cv"
    write_outputs(built_profile(model), folder)
    assert {p.name for p in folder.iterdir()} == {"text.txt", "profile.json", "review.md"}
    assert cli.main(["recheck", "--profile-model", str(FIXTURES / "profile.yaml"), "--out", str(tmp_path / "out")]) == 0
    assert "cv:" in capsys.readouterr().out and (tmp_path / "out" / "summary.md").exists()
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o700  # personal data: the owner's only


def test_the_upload_page_reads_a_cv_and_keeps_the_run(model, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from asas_cv_profile import upload_router

    def answer(call, s):
        if s.name == "Name":
            return {"full_name": "Sara Example", "lines": [[1, 1]]}
        return None

    app = FastAPI()
    app.include_router(upload_router(ProfileBuilder(model, Scripted(model, answer)), tmp_path))
    client = TestClient(app)
    assert "<html" in client.get("/").text
    cv = docx("Sara Example", "Senior Data Analyst at Example Co since 2019, reporting for twelve departments across the group.",
              "Built the monthly reporting pipeline and trained the analysts who run it.")
    got = client.post("/api/parse", files={"file": ("cv.docx", cv)}, data={"as_of": "2026-10-28"})
    assert got.status_code == 200
    body = got.json()
    assert body["profile"]["Identity"]["Name"]["fields"]["Full name"]["check"] == "verified"
    assert client.get("/api/runs").json() == [{"run": body["run"]}]
    assert json.loads(client.get(f"/files/{body['run']}/profile.json").text)["file"] == "cv.docx"
    assert client.get("/files/../../etc/passwd").status_code == 404
    refused = client.post("/api/parse", files={"file": ("cv.docx", docx("Page 1"))})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "document_too_little_text"


def test_the_upload_page_refuses_other_sites_and_other_hosts(model, tmp_path):
    from fastapi.testclient import TestClient

    from asas_cv_profile import upload_router
    from asas_cv_profile.web import local_app

    def answer(call, s):
        return {"full_name": "Sara Example", "lines": [[1, 1]]} if s.name == "Name" else None

    router = upload_router(ProfileBuilder(model, Scripted(model, answer)), tmp_path)
    cv = docx("Sara Example", "Senior Data Analyst at Example Co since 2019, reporting for twelve departments across the group.",
              "Built the monthly reporting pipeline and trained the analysts who run it.")
    client = TestClient(local_app(router), base_url="http://127.0.0.1:8765")
    for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                    {"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"}):
        assert client.post("/api/parse", files={"file": ("cv.docx", cv)}, headers=headers).status_code == 403
    assert not (tmp_path / "uploads").exists()  # nothing read, nothing kept
    assert TestClient(local_app(router), base_url="http://rebound.example").get("/").status_code == 400
    ok = client.post("/api/parse", files={"file": ("my cv.Final.DOCX", cv)},
                     headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200
    assert [p.name for p in (tmp_path / "uploads" / ok.json()["run"]).glob("original*")] == ["original.docx"]


def test_the_release_comes_from_a_store_by_label_and_must_match_the_model(tmp_path):
    """--store/--label: the profile builder's release is read from a release store, as asas-agent
    hosts read their agents; a label that points at another model's release is refused."""
    import asyncio
    from types import SimpleNamespace

    from asas_agent import FileStore

    from asas_cv_profile.calls import AGENT, default_release
    from asas_cv_profile.cli import _release

    store = FileStore(tmp_path / "releases")
    built = default_release("gpt-x")
    asyncio.run(store.publish(AGENT, built))
    asyncio.run(store.promote(AGENT, built["id"], "production"))
    args = SimpleNamespace(store=f"file:{tmp_path / 'releases'}", label="production", agents=None, effort=None)
    assert _release("gpt-x", args)["id"] == built["id"]
    with pytest.raises(ValueError):
        _release("another-model", args)


def test_recheck_keeps_personal_facts_and_every_line_when_asked(model, tmp_path, capsys):
    """recheck rebuilds other information without a model call: with --keep-all and
    --keep-personal it keeps what a build with them kept, so a saved run can be brought up to them."""
    built = built_profile(model)
    built["text"]["lines"] += ["PERSONAL DETAILS", "Visa status: residence permit", "Marital status: married"]
    run_dir = tmp_path / "runs"
    write_outputs(built, run_dir / "cv")
    assert cli.main(["recheck", "--profile-model", str(FIXTURES / "profile.yaml"), "--out", str(run_dir),
                     "--keep-all", "--keep-personal"]) == 0
    again = json.loads((run_dir / "cv" / "profile.json").read_text())
    blocks = again["profile"]["Identity"]["Other information"]
    n = len(built["text"]["lines"])
    assert any(b.get("restricted") and b["lines"] == [n] for b in blocks)
    assert any(n - 1 in b["lines"] and not b.get("restricted") for b in blocks)


def test_effort_replaces_the_folders_and_the_release_is_what_a_folder_with_it_would_build(tmp_path):
    """--effort: one prompt for two models. The release equals, id and all, the one a copy of
    the folder with that effort would build; a store's pinned release takes no --effort."""
    import shutil
    from types import SimpleNamespace

    from asas_cv_profile.calls import AGENT, AGENTS, default_release
    from asas_cv_profile.cli import _release

    copy = tmp_path / "agents"
    shutil.copytree(AGENTS, copy)
    yaml_path = copy / AGENT / "agent.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("effort: low", "effort: none"))
    assert default_release("small-model", AGENTS, "none") == default_release("small-model", copy)
    assert default_release("big-model", AGENTS, "low") == default_release("big-model", AGENTS)
    with pytest.raises(ValueError):
        default_release("big-model", AGENTS, "maximal")
    with pytest.raises(SystemExit, match="--effort"):
        _release("big-model", SimpleNamespace(store="file:x", label="production", agents=None, effort="none"))
