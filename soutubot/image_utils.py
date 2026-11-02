"""Message image extraction helpers for MaiBot plugins."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any


def guess_image_format(image_base64: str) -> str:
    try:
        raw = base64.b64decode(image_base64[:64] + "==", validate=False)
    except Exception:
        return "png"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "gif"
    if raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
        return "webp"
    return "png"


def iter_component_dicts(raw_message: Any) -> list[dict[str, Any]]:
    if raw_message is None:
        return []
    if isinstance(raw_message, list):
        return [x for x in raw_message if isinstance(x, dict)]
    if isinstance(raw_message, dict):
        components = raw_message.get("components")
        if isinstance(components, list):
            return [x for x in components if isinstance(x, dict)]
        for key in ("items", "messages", "content", "message"):
            value = raw_message.get(key)
            if isinstance(value, list):
                nested: list[dict[str, Any]] = []
                for item in value:
                    if isinstance(item, dict):
                        if item.get("type"):
                            nested.append(item)
                        else:
                            nested.extend(iter_component_dicts(item))
                if nested:
                    return nested
    return []


def project_root_from_plugin_file(plugin_file: str | Path) -> Path:
    """plugins/<Name>/plugin.py -> project root."""
    return Path(plugin_file).resolve().parents[2]


def load_image_base64_by_hash(
    image_hash: str,
    *,
    project_root: Path,
) -> tuple[str, str] | None:
    digest = str(image_hash or "").strip()
    if not digest or len(digest) < 16:
        return None
    images_dir = project_root / "data" / "images"
    if not images_dir.is_dir():
        return None
    for path in images_dir.glob(f"{digest}.*"):
        if path.suffix.lower() in {".tmp", ".json", ".txt"}:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        fmt = path.suffix.lower().lstrip(".") or "png"
        if fmt == "jpg":
            fmt = "jpeg"
        return base64.b64encode(raw).decode("ascii"), fmt
    return None


def extract_images_from_message(
    message: dict[str, Any],
    *,
    max_count: int = 1,
    project_root: Path | None = None,
) -> list[dict[str, str]]:
    """Extract image base64 list from a serialized MaiBot / OneBot message."""
    images: list[dict[str, str]] = []
    if not isinstance(message, dict):
        return images

    # Prefer MaiBot raw_message; also accept OneBot-style message list.
    candidates: list[Any] = [message.get("raw_message"), message.get("message")]
    # Some wrappers nest under data.
    data = message.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("raw_message"), data.get("message")])

    for candidate in candidates:
        for component in iter_component_dicts(candidate):
            ctype = str(component.get("type") or "").lower()
            data_obj = component.get("data") if isinstance(component.get("data"), dict) else {}
            # OneBot often uses type=image + data.file/url/base64
            if ctype not in {"image", "img", "picture"} and not (
                isinstance(data_obj, dict)
                and (
                    data_obj.get("file")
                    or data_obj.get("url")
                    or data_obj.get("base64")
                    or data_obj.get("binary_data_base64")
                )
            ):
                # Still allow components that already look like image payloads.
                if not (
                    component.get("binary_data_base64")
                    or component.get("image_base64")
                    or component.get("hash")
                    or component.get("binary_hash")
                ):
                    continue

            image_hash = str(
                component.get("hash")
                or component.get("binary_hash")
                or data_obj.get("hash")
                or ""
            ).strip()
            b64 = str(
                component.get("binary_data_base64")
                or component.get("image_base64")
                or data_obj.get("binary_data_base64")
                or data_obj.get("image_base64")
                or ""
            ).strip()
            # OneBot base64:// payload
            file_field = str(data_obj.get("file") or data_obj.get("base64") or "").strip()
            if not b64 and file_field.lower().startswith("base64://"):
                b64 = file_field.split("://", 1)[1].strip()
            elif not b64 and data_obj.get("base64"):
                b64 = str(data_obj.get("base64")).strip()

            fmt = ""
            if b64:
                if "," in b64 and b64.lower().startswith("data:"):
                    b64 = b64.split(",", 1)[1].strip()
                fmt = guess_image_format(b64)
            elif image_hash and project_root is not None:
                loaded = load_image_base64_by_hash(image_hash, project_root=project_root)
                if loaded:
                    b64, fmt = loaded
            if not b64:
                continue
            images.append(
                {
                    "base64": b64,
                    "format": fmt or "png",
                    "hash": image_hash,
                }
            )
            if len(images) >= max(1, int(max_count or 1)):
                return images
    return images


def peel_envelope(payload: Any) -> Any:
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            return current
        if "result" in current and len(current) <= 3:
            current = current.get("result")
            continue
        if "data" in current and "success" not in current and len(current) <= 3:
            current = current.get("data")
            continue
        return current
    return current


def extract_message_dict(payload: Any) -> dict[str, Any] | None:
    current = peel_envelope(payload)
    if not isinstance(current, dict):
        return None
    if current.get("success") is False:
        return None
    message = current.get("message")
    if isinstance(message, dict):
        return message
    if "raw_message" in current or "message_info" in current or "message_id" in current:
        return current
    nested = current.get("result")
    if isinstance(nested, dict):
        if isinstance(nested.get("message"), dict):
            return nested["message"]
        if "raw_message" in nested or "message_info" in nested or "message" in nested:
            return nested
    data = current.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("message"), (dict, list)):
            return data
        if "raw_message" in data or "message_id" in data:
            return data
    return None
