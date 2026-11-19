from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile

from app.core.config import Settings
from app.services.storage import save_upload


async def test_upload_collision_preserves_the_original_audio(settings: Settings) -> None:
    file_id = uuid4()
    settings.upload_dir.mkdir()
    existing_file = settings.upload_dir / f"{file_id}.wav"
    existing_file.write_bytes(b"original audio")
    upload = UploadFile(BytesIO(b"new audio"), filename="new.wav")

    with pytest.raises(HTTPException) as exc:
        await save_upload(upload, file_id, settings)

    assert exc.value.status_code == 503
    assert existing_file.read_bytes() == b"original audio"
    assert upload.file.closed
