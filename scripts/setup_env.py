"""Create an ignored .env with random local secrets; never overwrite an existing one."""

import os
import secrets
from pathlib import Path


def open_private_file(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    content = (root / ".env.example").read_text(encoding="utf-8")
    password = secrets.token_hex(24)
    content = content.replace("JWT_SECRET=\n", f"JWT_SECRET={secrets.token_urlsafe(48)}\n")
    content = content.replace("POSTGRES_PASSWORD=\n", f"POSTGRES_PASSWORD={password}\n")
    content = content.replace("YOUR_URL_ENCODED_PASSWORD", password)
    try:
        with open(destination, "x", encoding="utf-8", opener=open_private_file) as output:
            output.write(content)
    except FileExistsError:
        raise SystemExit(".env already exists; it was not overwritten") from None
    print(
        "Created .env. Set AI_MODEL and the chosen provider's API key before processing lectures."
    )


if __name__ == "__main__":
    main()
