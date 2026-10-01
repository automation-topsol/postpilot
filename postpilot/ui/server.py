"""`postpilot ui` — the local weekly importer.

Runs on 127.0.0.1 only and is never deployed. It is a front end for exactly
one thing a person could do by hand — put files in Drive and type rows into
the Sheet — so it sits outside the delivery path entirely. It does not lease,
publish, or write `_State`; see `importer.py` for why it does not even run
`sync`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import shutil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from postpilot.config import REPO_ROOT, MissingSetting, Settings
from postpilot.importer import (
    DriveUploader,
    batch_id,
    existing_posts,
    import_reviews,
    load_brands,
)
from postpilot.logging import get_logger
from postpilot.plan import PlannedPost, Review, parse_plan, review_plan
from postpilot.sheets.client import SheetClient

log = get_logger(__name__)

BATCH_ROOT = REPO_ROOT / ".postpilot" / "batches"
INDEX = Path(__file__).with_name("index.html")
TEMPLATE = Path(__file__).with_name("plan-template.md")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class PostsIn(BaseModel):
    posts: list[dict]

    def planned(self) -> list[PlannedPost]:
        known = {f.name for f in dataclasses.fields(PlannedPost)}
        return [PlannedPost(**{k: v for k, v in p.items() if k in known}) for p in self.posts]


def _review_json(index: int, review: Review) -> dict:
    return {
        "index": index,
        "post": dataclasses.asdict(review.post),
        "errors": review.errors,
        "platform_errors": review.platform_errors,
        "warnings": review.warnings,
        "captions": review.captions,
        "duplicate_of": review.duplicate_of,
        "importable": review.importable,
    }


def _safe_name(name: str) -> str:
    """A path relative to the batch, sub-folders kept: `d02_p01/slide_01.png`.

    A carousel dropped as a folder is named by its path in the plan, so the
    folder must survive. Nothing may climb out of the batch or hide.
    """
    parts = [p.strip() for p in (name or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p not in ("", ".", "..")]
    if not parts or any(p.startswith(".") for p in parts):
        raise HTTPException(400, f"bad file name {name!r}")
    return "/".join(parts)


def _listing(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _save(root: Path, upload: UploadFile) -> None:
    path = root / _safe_name(upload.filename or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        shutil.copyfileobj(upload.file, out)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="PostPilot importer", docs_url=None, redoc_url=None)
    tz = settings.tunables.timezone

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse anything a web page elsewhere could trigger.

        A multipart POST is a "simple" request browsers send cross-origin
        without asking, so any website could otherwise poke this server. And a
        Host check stops DNS rebinding from dressing a remote page up as local.
        """
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in LOCAL_HOSTS:
            return JSONResponse({"detail": "local requests only"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin and urlparse(origin).hostname not in LOCAL_HOSTS:
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    def sheet() -> SheetClient:
        return SheetClient(settings.google_credentials, settings.sheet_id)

    def batch_dir(bid: str) -> Path:
        path = (BATCH_ROOT / bid).resolve()
        if path.parent != BATCH_ROOT.resolve() or not path.is_dir():
            raise HTTPException(404, "unknown batch — drop the files again")
        return path

    def review(bid: str, posts: list[PlannedPost]) -> tuple[list[Review], dict]:
        client = sheet()
        brands = load_brands(client)
        files = set(_listing(batch_dir(bid) / "files"))
        reviews = review_plan(posts, brands, files, existing_posts(client, brands, tz), tz)
        return reviews, brands

    def payload(bid: str, reviews: list[Review], problems: list[str] | None = None) -> dict:
        return {
            "batch": bid,
            "problems": problems or [],
            "files": _listing(batch_dir(bid) / "files"),
            "reviews": [_review_json(i, r) for i, r in enumerate(reviews)],
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX.read_text(encoding="utf-8")

    @app.get("/template.md")
    def template() -> FileResponse:
        return FileResponse(TEMPLATE, media_type="text/markdown", filename="plan-template.md")

    @app.get("/api/status")
    def status() -> dict:
        from postpilot.auth.google import load_credentials, signed_in_email

        out: dict = {"timezone": tz, "brands": [], "google": None, "error": ""}
        try:
            client = sheet()
            out["sheet"] = client.title
            out["brands"] = [
                {"slug": b.slug, "name": b.name, "platforms": [p.value for p in b.enabled_platforms]}
                for b in load_brands(client).values()
            ]
        except Exception as exc:
            out["error"] = f"cannot read the Sheet: {exc}"
        creds = load_credentials()
        if creds is not None:
            out["google"] = signed_in_email(creds) or "signed in"
        return out

    @app.post("/api/auth/google")
    def auth_google() -> dict:
        from postpilot.auth.google import sign_in, signed_in_email

        try:
            creds = sign_in()
        except MissingSetting as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"google": signed_in_email(creds) or "signed in"}

    @app.post("/api/batch")
    async def new_batch(files: Annotated[list[UploadFile], File()]) -> dict:
        plans = [f for f in files if (f.filename or "").lower().endswith(".md")]
        media = [f for f in files if f not in plans]
        if len(plans) != 1:
            raise HTTPException(400, f"drop exactly one .md plan (found {len(plans)})")

        bid = batch_id()
        root = BATCH_ROOT / bid
        (root / "files").mkdir(parents=True, exist_ok=True)
        text = (await plans[0].read()).decode("utf-8", errors="replace")
        (root / "plan.md").write_text(text, encoding="utf-8")
        for upload in media:
            _save(root / "files", upload)

        posts, problems = parse_plan(text)
        reviews, _ = review(bid, posts)
        return payload(bid, reviews, problems)

    @app.post("/api/batch/{bid}/files")
    async def add_files(bid: str, files: Annotated[list[UploadFile], File()]) -> dict:
        """Add files forgotten in the first drop, without starting over."""
        target = batch_dir(bid) / "files"
        for upload in files:
            _save(target, upload)
        return {"files": _listing(target)}

    @app.post("/api/batch/{bid}/review")
    def re_review(bid: str, body: PostsIn) -> dict:
        posts = body.planned()
        reviews, _ = review(bid, posts)
        return payload(bid, reviews)

    @app.get("/api/batch/{bid}/file/{name:path}")
    def file(bid: str, name: str) -> FileResponse:
        path = batch_dir(bid) / "files" / _safe_name(name)
        if not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path)

    @app.post("/api/batch/{bid}/import")
    def do_import(bid: str, body: PostsIn) -> dict:
        from postpilot.auth.google import load_credentials

        creds = load_credentials()
        if creds is None:
            raise HTTPException(401, "sign in with Google first")

        posts = body.planned()
        reviews, brands = review(bid, posts)  # re-check against the Sheet as it is NOW
        result = import_reviews(
            reviews,
            files_dir=batch_dir(bid) / "files",
            brands=brands,
            client=sheet(),
            uploader=DriveUploader(creds),
        )
        log.info("import %s: %s", bid, result.rows_added)
        return dataclasses.asdict(result) | {"imported_at": dt.datetime.now(dt.UTC).isoformat()}

    return app


def serve(settings: Settings, *, port: int, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 127.0.0.1, never 0.0.0.0: this holds a Drive token and writes the Sheet.
    uvicorn.run(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
