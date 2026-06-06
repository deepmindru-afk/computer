"""File browser API - directory listing endpoint."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/workspace/files", tags=["workspace-files"])


class FileEntry(BaseModel):
    name: str
    type: str  # "directory" | "file" | "symlink"
    size: Optional[int] = None
    modified: Optional[str] = None


class DirectoryListing(BaseModel):
    path: str
    entries: list[FileEntry]


@router.get("", response_model=DirectoryListing)
async def list_directory(path: str = Query(..., description="Absolute path to list")):
    """Return the contents of a directory, sorted dirs-first then alphabetical."""

    target = Path(path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {path}")

    entries: list[FileEntry] = []

    try:
        for item in target.iterdir():
            try:
                st = item.stat()
                modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()

                if item.is_symlink():
                    entry_type = "symlink"
                elif item.is_dir():
                    entry_type = "directory"
                else:
                    entry_type = "file"

                entries.append(
                    FileEntry(
                        name=item.name,
                        type=entry_type,
                        size=st.st_size if entry_type == "file" else None,
                        modified=modified,
                    )
                )
            except (PermissionError, OSError):
                # Skip items we can't stat
                entries.append(FileEntry(name=item.name, type="file"))
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {path}")

    # Sort: directories first, then files, alphabetical within each group
    type_order = {"directory": 0, "symlink": 1, "file": 2}
    entries.sort(key=lambda e: (type_order.get(e.type, 2), e.name.lower()))

    return DirectoryListing(path=str(target), entries=entries)


# ── File content reading ─────────────────────────────────────────

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

# Extensions we know are text
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".svelte",
    ".css", ".html", ".htm", ".json", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh",
    ".fish", ".env", ".gitignore", ".dockerignore", ".editorconfig",
    ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".rb",
    ".php", ".pl", ".swift", ".kt", ".scala", ".sql", ".r",
    ".lua", ".vim", ".el", ".ex", ".exs", ".erl", ".hs",
    ".ml", ".clj", ".lisp", ".csv", ".tsv", ".log", ".diff",
    ".patch", ".makefile", ".cmake", ".gradle", ".sbt",
    ".tf", ".hcl", ".nix", ".lock", ".svg",
}


class FileContent(BaseModel):
    path: str
    name: str
    size: int
    binary: bool
    content: Optional[str] = None
    language: Optional[str] = None


def _detect_language(name: str) -> Optional[str]:
    """Map file extension to a language identifier for syntax highlighting."""
    ext = Path(name).suffix.lower()
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".svelte": "svelte",
        ".css": "css", ".html": "html", ".json": "json",
        ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".md": "markdown", ".sh": "bash",
        ".bash": "bash", ".zsh": "bash", ".rs": "rust",
        ".go": "go", ".java": "java", ".c": "c", ".h": "c",
        ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby",
        ".php": "php", ".swift": "swift", ".kt": "kotlin",
        ".sql": "sql", ".r": "r", ".lua": "lua",
        ".dockerfile": "dockerfile", ".makefile": "makefile",
        ".tf": "hcl", ".hcl": "hcl", ".nix": "nix",
    }
    return lang_map.get(ext)


def _is_text_file(filepath: Path) -> bool:
    """Check if a file is likely text based on extension or content sniffing."""
    if filepath.suffix.lower() in TEXT_EXTENSIONS:
        return True
    # Also check common extensionless files
    if filepath.name.lower() in {
        "makefile", "dockerfile", "gemfile", "rakefile",
        "procfile", "license", "readme", "changelog",
    }:
        return True
    # Content sniff: read first 8KB and check for null bytes
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(8192)
            return b"\x00" not in chunk
    except (OSError, PermissionError):
        return False


@router.get("/read", response_model=FileContent)
async def read_file(path: str = Query(..., description="Absolute path to file")):
    """Read the contents of a file."""

    target = Path(path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    size = target.stat().st_size

    if size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size} bytes). Max is {MAX_FILE_SIZE} bytes.",
        )

    is_text = _is_text_file(target)

    if is_text:
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as e:
            raise HTTPException(status_code=403, detail=str(e))
    else:
        content = None

    return FileContent(
        path=str(target),
        name=target.name,
        size=size,
        binary=not is_text,
        content=content,
        language=_detect_language(target.name) if is_text else None,
    )


# ── File writing ─────────────────────────────────────────────────


class WriteFileRequest(BaseModel):
    path: str
    content: str


@router.post("/write")
async def write_file(req: WriteFileRequest):
    """Write content to a file. Creates the file (and parent dirs) if it doesn't exist."""

    target = Path(req.path).resolve()

    if target.exists() and not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {req.path}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(req.content, encoding="utf-8")
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    return {"status": "saved", "path": str(target), "size": target.stat().st_size}


# ── File search ──────────────────────────────────────────────────


SEARCH_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".svelte-kit", ".next",
    "dist", "build", ".venv", "venv", ".tox", ".mypy_cache",
    ".pytest_cache", ".eggs", "*.egg-info", ".DS_Store",
}


class SearchResult(BaseModel):
    path: str
    name: str
    type: str  # "file" | "directory"


class SearchResponse(BaseModel):
    results: list[SearchResult]


@router.get("/search", response_model=SearchResponse)
async def search_files(
    query: str = Query(..., description="Search term"),
    path: str = Query(..., description="Root path to search"),
    limit: int = Query(20, description="Max results"),
):
    """Fuzzy filename search. Walks tree, matches basename case-insensitively."""

    root = Path(path).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    query_lower = query.lower()
    results: list[SearchResult] = []

    def walk(directory: Path, depth: int = 0):
        if depth > 8 or len(results) >= limit:
            return
        try:
            for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
                if item.name in SEARCH_IGNORE_DIRS or item.name.startswith("."):
                    continue
                if len(results) >= limit:
                    return

                if query_lower in item.name.lower():
                    results.append(SearchResult(
                        path=str(item),
                        name=item.name,
                        type="directory" if item.is_dir() else "file",
                    ))

                if item.is_dir():
                    walk(item, depth + 1)
        except (PermissionError, OSError):
            pass

    walk(root)
    return SearchResponse(results=results)


# ── File management ──────────────────────────────────────────────


class CreateRequest(BaseModel):
    path: str
    type: str = "file"  # "file" | "directory"


@router.post("/create")
async def create_item(req: CreateRequest):
    """Create a new file or directory."""
    target = Path(req.path).resolve()

    if target.exists():
        raise HTTPException(status_code=409, detail=f"Already exists: {req.path}")

    try:
        if req.type == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    return {"status": "created", "path": str(target), "type": req.type}


class MoveRequest(BaseModel):
    source: str
    destination: str


@router.post("/move")
async def move_item(req: MoveRequest):
    """Move or rename a file/directory."""
    src = Path(req.source).resolve()
    dst = Path(req.destination).resolve()

    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Source not found: {req.source}")

    # If destination is a directory, move into it
    if dst.is_dir():
        dst = dst / src.name

    if dst.exists():
        raise HTTPException(status_code=409, detail=f"Destination exists: {dst}")

    try:
        src.rename(dst)
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    return {"status": "moved", "source": str(src), "destination": str(dst)}


class DeleteRequest(BaseModel):
    path: str


@router.post("/delete")
async def delete_item(req: DeleteRequest):
    """Delete a file or directory."""
    import shutil

    target = Path(req.path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {req.path}")

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    return {"status": "deleted", "path": str(target)}


from fastapi import UploadFile, File as FastAPIFile, Form


@router.post("/upload")
async def upload_file(
    file: UploadFile = FastAPIFile(...),
    directory: str = Form(...),
):
    """Upload a file to a directory."""
    target_dir = Path(directory).resolve()

    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {directory}")

    target = target_dir / file.filename
    try:
        content = await file.read()
        target.write_bytes(content)
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=403, detail=str(e))

    return {"status": "uploaded", "path": str(target), "size": len(content)}


# ── File viewing (inline) ────────────────────────────────────────

import mimetypes

from fastapi.responses import FileResponse


@router.get("/view")
async def view_file(path: str = Query(..., description="Absolute path to file")):
    """Serve a file with correct Content-Type for inline browser rendering.

    Unlike /download (which forces application/octet-stream to trigger a
    save dialog), this endpoint lets the browser render the file inline —
    used by the frontend for image, PDF, video, and audio previews.
    """
    target = Path(path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    media_type, _ = mimetypes.guess_type(str(target))

    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        # No filename= → browser renders inline instead of downloading
    )


# ── File download ────────────────────────────────────────────────


@router.get("/download")
async def download_file(path: str = Query(..., description="Absolute path to file")):
    """Download a file."""
    target = Path(path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


# ── Archive (zip) ────────────────────────────────────────────────

import io
import zipfile

from fastapi.responses import StreamingResponse


class ArchiveRequest(BaseModel):
    paths: list[str]


@router.post("/archive")
async def archive_files(req: ArchiveRequest):
    """Create a zip archive from a list of file/directory paths."""
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for raw_path in req.paths:
            target = Path(raw_path).resolve()
            if not target.exists():
                continue

            if target.is_file():
                zf.write(target, target.name)
            elif target.is_dir():
                for child in target.rglob("*"):
                    if child.is_file():
                        arcname = str(child.relative_to(target.parent))
                        zf.write(child, arcname)

    buf.seek(0)

    # Derive a sensible filename
    if len(req.paths) == 1:
        name = Path(req.paths[0]).name + ".zip"
    else:
        name = "archive.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ── Static file serving (path-based) ────────────────────────────


@router.get("/serve/{file_path:path}")
async def serve_static(file_path: str):
    """Serve a file using path-based routing with correct MIME type.

    Unlike /view (query-string based), this uses path segments so
    HTML files in iframes can resolve relative CSS/JS/image references.
    E.g. /api/files/serve/home/user/project/index.html
    """
    # Windows paths arrive as "C:/Users/..." - don't prepend /
    if len(file_path) >= 2 and file_path[1] == ':':
        target = Path(file_path).resolve()
    else:
        target = Path("/" + file_path).resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {file_path}")

    media_type, _ = mimetypes.guess_type(str(target))

    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
    )
