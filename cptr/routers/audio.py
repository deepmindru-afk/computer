"""Audio router: STT transcription via Whisper-compatible API.

Supports optional pydub/ffmpeg for compressing and splitting long recordings.
If pydub is not installed, raw audio is sent directly (works for < 25MB files).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile

from cptr.models import Config
from cptr.utils.config import _get_jwt_secret, check_access
from cptr.utils.crypto import decrypt_key

logger = logging.getLogger(__name__)

# Optional pydub for long audio handling
try:
    from pydub import AudioSegment  # type: ignore[import-untyped]

    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False

router = APIRouter(prefix="/api/audio", tags=["audio"])

COOKIE_NAME = "cptr_session"
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB Whisper API limit


def _get_user(request: Request) -> str:
    """Extract user_id from cookie, raise 401 if not authenticated."""
    token = request.cookies.get(COOKIE_NAME)
    client_host = request.client.host if request.client else "127.0.0.1"
    auth = check_access(client_host=client_host, jwt_token=token)
    if not auth or not auth.user_id:
        raise HTTPException(401, "authentication required")
    return auth.user_id


# ── Audio processing (optional, requires pydub + ffmpeg) ─────────


def compress_audio(file_path: str) -> str:
    """Compress audio to 16kHz mono 32kbps MP3 if larger than MAX_FILE_SIZE."""
    if not HAS_PYDUB or os.path.getsize(file_path) <= MAX_FILE_SIZE:
        return file_path

    audio = AudioSegment.from_file(file_path)
    audio = audio.set_frame_rate(16000).set_channels(1)

    base, _ = os.path.splitext(file_path)
    compressed_path = f"{base}_compressed.mp3"
    audio.export(compressed_path, format="mp3", bitrate="32k")
    return compressed_path


def split_audio(file_path: str, max_bytes: int) -> list[str]:
    """Split audio into chunks not exceeding max_bytes.

    Returns a list of chunk file paths. If audio fits, returns [file_path].
    """
    file_size = os.path.getsize(file_path)
    if file_size <= max_bytes:
        return [file_path]

    if not HAS_PYDUB:
        return [file_path]  # Can't split without pydub

    audio = AudioSegment.from_file(file_path)
    duration_ms = len(audio)

    approx_chunk_ms = max(int(duration_ms * (max_bytes / file_size)) - 1000, 1000)
    chunks: list[str] = []
    start = 0
    i = 0
    base, _ = os.path.splitext(file_path)

    while start < duration_ms:
        end = min(start + approx_chunk_ms, duration_ms)
        chunk = audio[start:end]
        chunk_path = f"{base}_chunk_{i}.mp3"
        chunk.export(chunk_path, format="mp3", bitrate="32k")

        # Halve chunk duration if still too large
        while os.path.getsize(chunk_path) > max_bytes and (end - start) > 5000:
            end = start + ((end - start) // 2)
            chunk = audio[start:end]
            chunk.export(chunk_path, format="mp3", bitrate="32k")

        if os.path.getsize(chunk_path) > max_bytes:
            os.remove(chunk_path)
            raise RuntimeError("Audio chunk cannot be reduced below max file size.")

        chunks.append(chunk_path)
        start = end
        i += 1

    return chunks


# ── Transcription ────────────────────────────────────────────────


async def _transcribe_chunk(
    data: bytes,
    filename: str,
    content_type: str,
    base_url: str,
    api_key: str,
    model: str,
) -> str:
    """Send a single audio chunk to the STT API."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(120)) as client:
        resp = await client.post(
            f"{base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, data, content_type)},
            data={"model": model},
        )
        resp.raise_for_status()
    return resp.json().get("text", "")


@router.post("/transcribe")
async def transcribe(file: UploadFile, request: Request):
    """Send audio to a Whisper-compatible STT API, return transcript text.

    If pydub + ffmpeg are available and the file is large, it will be
    compressed and split into chunks that are transcribed concurrently.
    """
    _get_user(request)

    api_key_encrypted = await Config.get("audio.stt_api_key")
    if not api_key_encrypted:
        raise HTTPException(
            400,
            "Speech-to-text not configured. Set up in Settings → Audio.",
        )

    api_key = decrypt_key(api_key_encrypted, _get_jwt_secret())
    base_url = (await Config.get("audio.stt_base_url")) or "https://api.openai.com/v1"
    model = (await Config.get("audio.stt_model")) or "whisper-1"

    raw_data = await file.read()
    filename = file.filename or "audio.webm"
    content_type = file.content_type or "audio/webm"

    # Small file: send directly, no temp files needed
    if len(raw_data) <= MAX_FILE_SIZE or not HAS_PYDUB:
        try:
            text = await _transcribe_chunk(raw_data, filename, content_type, base_url, api_key, model)
            return {"text": text}
        except httpx.HTTPStatusError as exc:
            detail = f"STT API error: {exc.response.status_code}"
            if len(raw_data) > MAX_FILE_SIZE and not HAS_PYDUB:
                detail += ". Recording is too large. Install ffmpeg and pydub for automatic splitting."
            logger.warning("[transcribe] %s: %s", detail, exc.response.text[:500])
            raise HTTPException(502, detail)
        except httpx.ConnectError:
            raise HTTPException(502, "Could not connect to STT API")

    # Large file: compress → split → transcribe chunks concurrently
    tmp_dir = tempfile.mkdtemp(prefix="cptr-stt-")
    file_path = os.path.join(tmp_dir, filename)
    Path(file_path).write_bytes(raw_data)

    chunk_paths: list[str] = []
    try:
        # Compress
        compressed = await asyncio.to_thread(compress_audio, file_path)

        # Split
        chunk_paths = await asyncio.to_thread(split_audio, compressed, MAX_FILE_SIZE)

        # Transcribe all chunks concurrently
        async def _do_chunk(path: str) -> str:
            chunk_data = await asyncio.to_thread(Path(path).read_bytes)
            chunk_name = os.path.basename(path)
            return await _transcribe_chunk(chunk_data, chunk_name, "audio/mpeg", base_url, api_key, model)

        tasks = [_do_chunk(p) for p in chunk_paths]
        # Use gather to preserve order (as_completed doesn't guarantee it)
        results = await asyncio.gather(*tasks)

        return {"text": " ".join(r for r in results if r)}

    except httpx.HTTPStatusError as exc:
        logger.warning("[transcribe] STT API error %s: %s", exc.response.status_code, exc.response.text[:500])
        raise HTTPException(502, f"STT API error: {exc.response.status_code}")
    except httpx.ConnectError:
        raise HTTPException(502, "Could not connect to STT API")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    finally:
        # Clean up temp files
        for p in chunk_paths:
            if p != file_path and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # Clean up compressed file if different from original
        if "compressed" in locals() and compressed != file_path and os.path.isfile(compressed):
            try:
                os.remove(compressed)
            except OSError:
                pass
        try:
            os.remove(file_path)
            os.rmdir(tmp_dir)
        except OSError:
            pass
