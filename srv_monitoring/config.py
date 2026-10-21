from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("MONITORING_ENV_FILE", str(ROOT_DIR / ".env"))).expanduser()
load_dotenv(ENV_FILE, override=False)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(value)
    try:
        path.chmod(0o600)
    except OSError:
        # Windows permissions are inherited from the containing directory.
        pass


def _local_secret(data_dir: Path, filename: str, env_name: str, create: Callable[[], str]) -> str:
    supplied = os.getenv(env_name)
    if supplied:
        return supplied.strip()
    path = data_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    generated = create()
    try:
        _write_private(path, generated)
        return generated
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    database_path: Path
    host: str
    port: int
    public_base_url: str | None
    admin_user: str
    admin_password: str
    session_secret: bytes
    encryption_key: bytes
    trusted_hosts: tuple[str, ...]
    ssl_certfile: str | None
    ssl_keyfile: str | None
    secure_cookies: bool
    require_https: bool
    max_request_bytes: int


def load_settings() -> Settings:
    data_dir = Path(os.getenv("MONITORING_DATA_DIR", str(ROOT_DIR / "data"))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not password:
        bootstrap_path = data_dir / "initial-admin-password.txt"
        was_generated = not bootstrap_path.exists()
        password = _local_secret(
            data_dir,
            "initial-admin-password.txt",
            "ADMIN_PASSWORD",
            lambda: secrets.token_urlsafe(24),
        )
        try:
            if was_generated:
                print(
                    "[SRV-monitoring] ADMIN_PASSWORD is not set. A one-time bootstrap password "
                    f"was saved to: {bootstrap_path}"
                )
            else:
                print(f"[SRV-monitoring] ADMIN_PASSWORD is not set; using {bootstrap_path}")
        except Exception:
            # PyInstaller --noconsole has no stdout; the bootstrap file remains available.
            pass

    session_secret = _local_secret(
        data_dir,
        "session.key",
        "APP_SESSION_SECRET",
        lambda: secrets.token_urlsafe(48),
    ).encode("utf-8")

    raw_encryption_key = _local_secret(
        data_dir,
        "encryption.key",
        "MONITORING_ENCRYPTION_KEY",
        lambda: Fernet.generate_key().decode("ascii"),
    )
    try:
        encryption_key = raw_encryption_key.encode("ascii")
        Fernet(encryption_key)
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(
            "MONITORING_ENCRYPTION_KEY must be a valid Fernet key; "
            "do not replace it after sources have been configured."
        ) from exc

    trusted_hosts = tuple(
        item.strip()
        for item in os.getenv("TRUSTED_HOSTS", "*").split(",")
        if item.strip()
    ) or ("*",)
    certfile = os.getenv("SSL_CERTFILE", "").strip() or None
    keyfile = os.getenv("SSL_KEYFILE", "").strip() or None
    host = os.getenv("APP_HOST", "0.0.0.0").strip()
    require_https_default = host not in {"127.0.0.1", "localhost", "::1"}
    require_https = _env_bool("REQUIRE_HTTPS", require_https_default)
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None
    if public_base_url:
        public_parsed = urlparse(public_base_url)
        if public_parsed.scheme not in {"http", "https"} or not public_parsed.netloc:
            raise RuntimeError("PUBLIC_BASE_URL must be a full http:// or https:// URL")
        if require_https and public_parsed.scheme != "https":
            raise RuntimeError("PUBLIC_BASE_URL must use https:// when REQUIRE_HTTPS=true")
    if bool(certfile) != bool(keyfile):
        raise RuntimeError("Set both SSL_CERTFILE and SSL_KEYFILE, or leave both empty")

    return Settings(
        root_dir=ROOT_DIR,
        data_dir=data_dir,
        database_path=Path(
            os.getenv("MONITORING_DB_PATH", str(data_dir / "monitoring.sqlite3"))
        ).expanduser().resolve(),
        host=host,
        port=int(os.getenv("APP_PORT", "8765")),
        public_base_url=public_base_url,
        admin_user=os.getenv("ADMIN_USER", "admin").strip() or "admin",
        admin_password=password,
        session_secret=session_secret,
        encryption_key=encryption_key,
        trusted_hosts=trusted_hosts,
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
        secure_cookies=_env_bool("SECURE_COOKIES", False) or bool(certfile),
        require_https=require_https,
        max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(256 * 1024))),
    )
