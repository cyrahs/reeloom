from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_MAX_DOTENV_BYTES = 64 * 1024
_MAX_API_KEY_BYTES = 4 * 1024
_MAX_BASE_URL_BYTES = 2 * 1024
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_DOTENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_REASONING_EFFORT",
    }
)


class OpenAILiveConfigurationError(RuntimeError):
    """A stable configuration failure that never contains secret values."""


@dataclass(frozen=True, slots=True)
class OpenAILiveConfiguration:
    api_key: str
    base_url: str
    model_name: str | None
    reasoning_effort: str | None


def project_dotenv_path(script_path: Path) -> Path:
    return script_path.resolve(strict=True).parent.parent / ".env"


def _validate_api_key(value: str) -> str:
    key = value.strip()
    if (
        not key
        or len(key.encode("utf-8")) > _MAX_API_KEY_BYTES
        or any(character.isspace() for character in key)
        or any(unicodedata.category(character).startswith("C") for character in key)
    ):
        raise OpenAILiveConfigurationError("invalid_openai_api_key")
    return key


def _validate_base_url(value: str) -> str:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate.encode("utf-8")) > _MAX_BASE_URL_BYTES
        or any(unicodedata.category(character).startswith("C") for character in candidate)
    ):
        raise OpenAILiveConfigurationError("invalid_openai_base_url")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        raise OpenAILiveConfigurationError("invalid_openai_base_url") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise OpenAILiveConfigurationError("invalid_openai_base_url")
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_model_name(value: str) -> str:
    model_name = value.strip()
    if _MODEL_NAME.fullmatch(model_name) is None:
        raise OpenAILiveConfigurationError("invalid_openai_model")
    return model_name


def _validate_reasoning_effort(value: str) -> str:
    reasoning_effort = value.strip()
    if reasoning_effort not in _REASONING_EFFORTS:
        raise OpenAILiveConfigurationError("invalid_openai_reasoning_effort")
    return reasoning_effort


def parse_dotenv(contents: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        normalized_name = name.strip()
        if not separator or normalized_name not in _DOTENV_NAMES:
            continue
        if normalized_name in found:
            raise OpenAILiveConfigurationError(
                f"duplicate_{normalized_name.casefold()}"
            )
        value = raw_value.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise OpenAILiveConfigurationError("invalid_dotenv_value")
            value = value[1:-1]
        found[normalized_name] = value
    return found


def read_project_dotenv(path: Path) -> dict[str, str]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OpenAILiveConfigurationError("no_nofollow_support")
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        file_descriptor = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError:
        raise OpenAILiveConfigurationError("dotenv_open_failed") from None
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_DOTENV_BYTES:
            raise OpenAILiveConfigurationError("invalid_dotenv_file")
        contents = bytearray()
        while len(contents) <= _MAX_DOTENV_BYTES:
            chunk = os.read(
                file_descriptor,
                min(8_192, _MAX_DOTENV_BYTES + 1 - len(contents)),
            )
            if not chunk:
                break
            contents.extend(chunk)
        if len(contents) > _MAX_DOTENV_BYTES:
            raise OpenAILiveConfigurationError("dotenv_too_large")
    finally:
        os.close(file_descriptor)
    try:
        decoded = bytes(contents).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OpenAILiveConfigurationError("invalid_dotenv_encoding") from None
    return parse_dotenv(decoded)


def load_openai_live_configuration(
    *,
    dotenv_path: Path,
    environ: Mapping[str, str] | None = None,
    model_name_override: str | None = None,
    reasoning_effort_override: str | None = None,
) -> OpenAILiveConfiguration:
    source = os.environ if environ is None else environ
    process_api_key = source.get("OPENAI_API_KEY", "").strip()
    process_base_url = source.get("OPENAI_BASE_URL", "").strip()
    process_model_name = (
        (model_name_override or "").strip()
        or source.get("OPENAI_MODEL", "").strip()
    )
    process_reasoning_effort = (
        (reasoning_effort_override or "").strip()
        or source.get("OPENAI_REASONING_EFFORT", "").strip()
    )
    dotenv = (
        read_project_dotenv(dotenv_path)
        if (
            not process_api_key
            or not process_base_url
            or not process_model_name
            or not process_reasoning_effort
        )
        else {}
    )
    raw_api_key = process_api_key or dotenv.get("OPENAI_API_KEY", "")
    raw_base_url = (
        process_base_url
        or dotenv.get("OPENAI_BASE_URL", "")
        or DEFAULT_OPENAI_BASE_URL
    )
    raw_model_name = process_model_name or dotenv.get("OPENAI_MODEL", "")
    raw_reasoning_effort = process_reasoning_effort or dotenv.get(
        "OPENAI_REASONING_EFFORT", ""
    )
    if not raw_api_key:
        raise OpenAILiveConfigurationError("missing_openai_api_key")
    return OpenAILiveConfiguration(
        api_key=_validate_api_key(raw_api_key),
        base_url=_validate_base_url(raw_base_url),
        model_name=(
            _validate_model_name(raw_model_name) if raw_model_name else None
        ),
        reasoning_effort=(
            _validate_reasoning_effort(raw_reasoning_effort)
            if raw_reasoning_effort
            else None
        ),
    )
