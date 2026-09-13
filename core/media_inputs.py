"""Local bounded image resizing and audio duration inspection."""

import asyncio
import hashlib
import json
import math
import tempfile
import wave
from pathlib import Path


async def prepare_input(path, kind, config):
    """Inspect local media without any additional model or provider request.

    Args:
        path: Resolved attachment path.
        kind: Image or audio.
        config: Effective input and cache constraints.

    Returns:
        Prepared path, rounded audio seconds and cache identity.

    Raises:
        ValueError: Audio duration cannot be established or exceeds the limit.
    """
    seconds = 0
    digest = str(path)
    if config["media_cache_days"]:
        # The caller has already enforced a 10 MiB file limit.
        digest = hashlib.sha256(
            await asyncio.to_thread(Path(path).read_bytes)
        ).hexdigest()
    if kind == "audio" and any(
        config[k] or config.get("_media_global", {}).get(k)
        for k in ("audio_max_seconds", "audio_daily_seconds")
    ):
        try:
            seconds = await asyncio.to_thread(wav_seconds, path)
        except (wave.Error, EOFError):
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                output, _ = await asyncio.wait_for(process.communicate(), 10)
                seconds = math.ceil(float(json.loads(output)["format"]["duration"]))
                if process.returncode or seconds <= 0:
                    raise ValueError
            except (OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
                raise ValueError(
                    "Audio duration unavailable; skipped under duration limits"
                ) from exc
            finally:
                if process is not None and process.returncode is None:
                    process.kill()
                    await process.wait()
        if config["audio_max_seconds"] and seconds > config["audio_max_seconds"]:
            raise ValueError("Audio duration limit exceeded")
    if kind == "image" and config["image_max_edge"]:
        path = await asyncio.to_thread(resize_image, path, config["image_max_edge"])
    return str(path), seconds, digest


def wav_seconds(path):
    """Read WAV duration off the event loop.

    Args:
        path: Local audio path.

    Returns:
        Rounded-up duration in seconds.
    """
    with wave.open(str(path), "rb") as audio:
        return math.ceil(audio.getnframes() / audio.getframerate())


def resize_image(path, edge):
    """Create a temporary resized copy without changing the original image.

    Args:
        path: Source image path.
        edge: Maximum width and height.

    Returns:
        Original path or temporary JPEG path.
    """
    try:
        from astrbot.core.utils.path_utils import get_astrbot_temp_path
    except ModuleNotFoundError:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        if max(source.size) <= edge:
            return str(path)
        prepared = ImageOps.exif_transpose(source)
        prepared.thumbnail((edge, edge))
        directory = Path(get_astrbot_temp_path())
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", prefix="memoir-", dir=directory, delete=False
        ) as output:
            try:
                prepared.convert("RGB").save(output, format="JPEG", quality=85)
            except BaseException:
                Path(output.name).unlink(missing_ok=True)
                raise
            return output.name
