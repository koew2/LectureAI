"""Check the effective Docker Compose configuration before a public launch."""

import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--env-file", ".env.production", "-f", "compose.production.yaml"]
AI_KEYS = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "orcarouter": "ORCA_KEY",
}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE | re.ASCII)


def public_hostname(value: str) -> bool:
    """Accept an ASCII DNS hostname suitable for a public Caddy certificate."""
    labels = value.split(".")
    if len(labels) < 2 or len(value) > 253 or not all(DNS_LABEL.fullmatch(x) for x in labels):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return True
    return False


def check_environment(
    api: dict[str, str], worker: dict[str, str], caddy: dict[str, str]
) -> list[str]:
    errors: list[str] = []
    domain = (caddy.get("PUBLIC_DOMAIN") or "").lower()
    if not public_hostname(domain) or domain == "example.com" or domain.endswith(".example.com"):
        errors.append("PUBLIC_DOMAIN must be your real hostname, without a scheme, port or path.")
    for name, env in (("ACME_EMAIL", caddy), ("ADMIN_EMAIL", api), ("SMTP_FROM", api)):
        address = env.get(name) or ""
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) or address.lower().endswith(
            "@example.com"
        ):
            errors.append(f"Set {name} to a real email address.")

    if not (api.get("SMTP_HOST") or "").strip():
        errors.append("Set SMTP_HOST so registration and notification emails can be delivered.")
    port = api.get("SMTP_PORT") or "587"
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        errors.append("SMTP_PORT must be a TCP port from 1 to 65535.")
    if bool(api.get("SMTP_USERNAME")) != bool(api.get("SMTP_PASSWORD")):
        errors.append("Set both SMTP_USERNAME and SMTP_PASSWORD when using SMTP authentication.")
    security = api.get("SMTP_SECURITY", "starttls")
    if security not in ("ssl", "starttls", "plain") or (
        security == "plain" and api.get("SMTP_USERNAME")
    ):
        errors.append("Use SMTP_SECURITY=ssl or starttls with SMTP authentication.")

    provider = worker.get("AI_PROVIDER", "openai")
    key_name = AI_KEYS.get(provider)
    if key_name is None:
        errors.append("AI_PROVIDER must be openai, deepseek, openrouter or orcarouter.")
    elif not (worker.get(key_name) or "").strip():
        errors.append(f"Set {key_name} for the selected AI provider.")
    if not (worker.get("AI_MODEL") or "").strip():
        errors.append("Set AI_MODEL to a model supported by the selected AI provider.")
    if len((api.get("JWT_SECRET") or "").encode()) < 32:
        errors.append("Set a separate JWT_SECRET of at least 32 bytes.")
    try:
        database = urlsplit(api.get("DATABASE_URL") or "")
        valid_database = database.hostname == "postgres" and database.port == 5432
    except ValueError:
        valid_database = False
    if not valid_database:
        errors.append("DATABASE_URL is malformed. Use a generated hexadecimal POSTGRES_PASSWORD.")
    return errors


def main() -> int:
    if not (ROOT / ".env.production").is_file():
        print("Create .env.production from .env.production.example first.", file=sys.stderr)
        return 1

    try:
        completed = subprocess.run(
            [*COMPOSE, "config", "--format", "json"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=30,
        )
        services = json.loads(completed.stdout)["services"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        print(
            "Compose could not resolve the production configuration. "
            "Check Docker and .env.production.",
            file=sys.stderr,
        )
        return 1

    api = services["api"]
    errors = check_environment(
        api["environment"], services["worker"]["environment"], services["caddy"]["environment"]
    )
    upload = next(volume for volume in api["volumes"] if volume["target"] == "/srv/eduflow/uploads")
    if not Path(upload["source"]).is_dir():
        errors.append("Create DATA_DIR/audio with write access for UID/GID 10001 before starting.")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(
        "Production configuration is ready for deployment. Verify DNS, SMTP and AI after startup."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
