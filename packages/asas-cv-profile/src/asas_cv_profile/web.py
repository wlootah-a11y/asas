"""The upload page: a CV in, the numbered text and the profile side by side.

:func:`upload_router` returns the page and its API as one ``APIRouter`` (the ``serve``
extra); the host applies its own guards when it includes it, and ``asas-cv-profile
serve`` mounts it on 127.0.0.1 for one person. Each upload is kept with its outputs
under ``<out>/uploads/<run id>/``, named by the time and the file's hash (a file name
often holds the person's name), so a run can be opened again and its files downloaded.
One CV is read at a time: a gateway throttles short bursts. An upload another site's page
sends is refused (OWASP's CSRF Cheat Sheet: Fetch Metadata, then Origin), and
:func:`local_app` answers only to the Host names of this machine, against DNS rebinding.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from asas_cv import DOCX_CONTENT_TYPE, PDF_CONTENT_TYPE, resolve_content_type
from asas_cv import MAX_DOCUMENT_BYTES as MAX_BYTES
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse

from .build import ProfileBuilder
from .lines import Refused
from .report import write_outputs

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"text.txt": "text/plain; charset=utf-8", "profile.json": "application/json", "review.md": "text/markdown; charset=utf-8"}
RUN_ID = re.compile(r"^\d{8}-\d{6}-[\w.-]{1,80}$")
#: the upload is kept under the type it was read as, never the browser's file name
SUFFIX = {PDF_CONTENT_TYPE: ".pdf", DOCX_CONTENT_TYPE: ".docx", "text/markdown": ".md"}
LOCAL_HOSTS = ["127.0.0.1", "localhost"]


def same_origin(request: Request) -> None:
    """A browser says where a request comes from; one from another site's page is refused.
    A client that sends neither header (the command line, curl) is not a browser."""
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site") or (
            origin is not None and urlsplit(origin).netloc != request.headers.get("host")):
        raise HTTPException(403, "Upload from this page only.")


def local_app(router: APIRouter) -> FastAPI:
    """``router`` served for one person on this machine: no API docs, and a request whose Host
    is not this machine's refused (Starlette's TrustedHostMiddleware)."""
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.include_router(router)
    return app


def upload_router(builder: ProfileBuilder, out: Path, *, packed: ProfileBuilder | None = None) -> APIRouter:
    """The page at ``/``, ``POST /api/parse``, the runs kept under ``out`` and their files.
    ``packed`` is the builder the page's "split" box uses (calls under the property budget)."""
    router = APIRouter()
    busy = asyncio.Lock()
    runs_dir = out / "uploads"

    def run_dir(run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise HTTPException(404)
        d = runs_dir / run_id
        if not d.is_dir():
            raise HTTPException(404)
        return d

    @router.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @router.post("/api/parse", dependencies=[Depends(same_origin)])
    async def parse(file: Annotated[UploadFile, File()], split: Annotated[bool, Form()] = False,
                    as_of: Annotated[str, Form()] = ""):
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"The file is over {MAX_BYTES // 2**20} MB.")
        name = Path(file.filename or "cv").name
        try:
            when = date.fromisoformat(as_of) if as_of else None
        except ValueError:
            raise HTTPException(400, "as_of must be YYYY-MM-DD.") from None
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha256(data).hexdigest()[:12]
        if busy.locked():
            raise HTTPException(429, "Another CV is being read. Try again when it finishes.")
        async with busy:
            try:
                built = await (packed if split and packed else builder).build(data, name, as_of=when)
            except Refused as exc:
                raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from None
            folder = runs_dir / run_id
            folder.mkdir(parents=True, mode=0o700)  # the CV and its profile: personal data, its owner's only
            runs_dir.chmod(0o700)
            (folder / ("original" + SUFFIX.get(resolve_content_type(name, None), ".txt"))).write_bytes(data)
            write_outputs(built, folder)
        return {"run": run_id, **built}

    @router.get("/api/runs")
    def runs() -> list[dict]:
        if not runs_dir.is_dir():
            return []
        return [{"run": d.name} for d in sorted(runs_dir.iterdir(), reverse=True) if (d / "profile.json").exists()][:50]

    @router.get("/api/runs/{run_id}")
    def run(run_id: str):
        return {"run": run_id, **json.loads((run_dir(run_id) / "profile.json").read_text(encoding="utf-8"))}

    @router.get("/files/{run_id}/{name}")
    def download(run_id: str, name: str):
        if name not in FILES:
            raise HTTPException(404)
        return FileResponse(run_dir(run_id) / name, media_type=FILES[name], filename=f"{run_id}-{name}")

    return router
