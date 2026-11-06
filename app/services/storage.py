"""Bounded streaming uploads using server-generated file names."""

from pathlib import Path
from uuid import UUID

import aiofiles
from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a"}
CHUNK_BYTES = 1024 * 1024


async def save_upload(upload: UploadFile, file_id: UUID, settings: Settings) -> Path:
    extension = Path(upload.filename or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Allowed formats: mp3, wav, m4a")
    directory = settings.upload_dir.resolve()
    await run_in_threadpool(directory.mkdir, parents=True, exist_ok=True)
    destination = directory / f"{file_id}{extension}"
    size = 0
    try:
        async with aiofiles.open(destination, "xb") as output:
            while chunk := await upload.read(CHUNK_BYTES):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Audio file is too large")
                await output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Audio file is empty")
    except BaseException:
        await run_in_threadpool(destination.unlink, missing_ok=True)
        raise
    finally:
        await upload.close()
    return destination
