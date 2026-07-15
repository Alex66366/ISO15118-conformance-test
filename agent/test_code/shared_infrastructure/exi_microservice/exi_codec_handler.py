"""
exi_codec_handler.py
=====================
Self-contained EXI bridge for the multi-agent test framework.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("EXI")


def _vendor_dir() -> Path:
    return Path(__file__).resolve().parent / "vendor"


def _find_exi_jar() -> Path:
    candidates = []
    env_jar = os.environ.get("EXI_JAR_PATH", "")
    if env_jar:
        candidates.append(Path(env_jar).expanduser().resolve())
    candidates.append(_vendor_dir() / "iso15118" / "shared" / "EXICodec.jar")
    legacy_root = os.environ.get("ISO15118_MASTER_PATH", "")
    if legacy_root:
        candidates.append(
            Path(legacy_root).expanduser().resolve() / "iso15118" / "shared" / "EXICodec.jar"
        )
    sibling = (
        _vendor_dir().parent.parent.parent.parent
        / "iso15118-master"
        / "iso15118"
        / "shared"
        / "EXICodec.jar"
    )
    candidates.append(sibling.resolve())
    candidates.append(
        Path("/home/dmc/桌面/platform/iso15118-master/iso15118/shared/EXICodec.jar").resolve()
    )
    for cand in candidates:
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "EXICodec.jar not found. Looked at:\n  "
        + "\n  ".join(str(c) for c in candidates)
        + "\n\nSet EXI_JAR_PATH or restore vendor/iso15118/shared/EXICodec.jar."
    )


def _find_python_bridge_dir() -> Path:
    candidates = []
    env_dir = os.environ.get("EXI_BRIDGE_DIR", "")
    if env_dir:
        candidates.append(Path(env_dir).expanduser().resolve())
    vendor_pkg = _vendor_dir() / "iso15118"
    if (vendor_pkg / "shared" / "exificient_exi_codec.py").exists():
        candidates.append(_vendor_dir())
    legacy_root = os.environ.get("ISO15118_MASTER_PATH", "")
    if legacy_root:
        candidates.append(Path(legacy_root).expanduser().resolve())
    sibling = _vendor_dir().parent.parent.parent.parent / "iso15118-master"
    candidates.append(sibling.resolve())
    candidates.append(Path("/home/dmc/桌面/platform/iso15118-master").resolve())
    for cand in candidates:
        if (cand / "iso15118" / "shared" / "exificient_exi_codec.py").exists():
            return cand
    raise FileNotFoundError(
        "iso15118 Python bridge not found. Looked at:\n  "
        + "\n  ".join(str(c) for c in candidates)
        + "\n\nSet EXI_BRIDGE_DIR or restore vendor/iso15118/."
    )


def _import_exificient_codec():
    bridge_dir = _find_python_bridge_dir()
    jar_path = _find_exi_jar()
    expected_jar = bridge_dir / "iso15118" / "shared" / "EXICodec.jar"
    if not expected_jar.exists():
        logger.info(f"[Bridge] Copying EXICodec.jar -> {expected_jar} (legacy bridge_dir)")
        try:
            expected_jar.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(jar_path, expected_jar)
        except OSError as e:
            logger.warning(f"[Bridge] Could not place JAR next to settings.py ({e}); relying on upstream path resolution")
    if str(bridge_dir) in sys.path:
        try:
            sys.path.remove(str(bridge_dir))
        except ValueError:
            pass
    sys.path.insert(0, str(bridge_dir))
    logger.debug(f"[Bridge] Added {bridge_dir} to sys.path (priority 0)")
    try:
        from iso15118.shared.exificient_exi_codec import ExificientEXICodec
    except ImportError as e:
        raise ImportError(
            f"Failed to import iso15118.shared.exificient_exi_codec.ExificientEXICodec: {e}\n"
            f"Make sure py4j is installed in the active Python environment."
        ) from e
    try:
        import iso15118.shared.settings as _settings
        _settings.JAR_FILE_PATH = str(jar_path)
    except Exception:
        pass
    return ExificientEXICodec, jar_path


class EXIError(Exception):
    pass


class EXIEncodingError(EXIError):
    pass


class EXIDecodingError(EXIError):
    pass


class ExiCodecHandler:
    def __init__(self):
        self._codec = None
        self._ExificientEXICodec = None
        self._jar_path = None
        self._started = False

    def _ensure_started(self) -> None:
        if self._started:
            return
        import threading
        with threading.Lock():
            if self._started:
                return
            self._do_start()
            self._started = True

    def _do_start(self) -> None:
        logging.getLogger("py4j").setLevel(logging.WARNING)
        ExificientEXICodec, jar_path = _import_exificient_codec()
        self._ExificientEXICodec = ExificientEXICodec
        self._jar_path = jar_path
        self._codec = ExificientEXICodec()
        try:
            version = self._codec.get_version()
        except Exception as e:
            version = f"(version probe failed: {e})"
        logger.info(f"[Bridge] ExificientEXICodec ready, version: {version}, jar={jar_path}")

    def try_encode_raw(self, payload, namespace):
        self._ensure_started()
        try:
            out = self._codec.encode(payload, namespace)
            return (out, None)
        except Exception as e:
            return (None, {"type": type(e).__name__, "message": str(e)})

    def encode(self, json_message, namespace):
        self._ensure_started()
        try:
            exi_bytes = self._codec.encode(json_message, namespace)
        except Exception as e:
            raise EXIEncodingError(
                f"EXI encoding failed for namespace '{namespace}': {e}"
            ) from e
        if exi_bytes is None:
            raise EXIEncodingError(
                f"EXI encoding returned null for namespace '{namespace}'"
            )
        if hasattr(exi_bytes, "__iter__"):
            return bytes(exi_bytes)
        return exi_bytes

    def decode(self, exi_bytes, namespace):
        self._ensure_started()
        try:
            json_str = self._codec.decode(exi_bytes, namespace)
        except Exception as e:
            raise EXIDecodingError(
                f"EXI decoding failed for namespace '{namespace}': {e}"
            ) from e
        if json_str is None:
            raise EXIDecodingError(
                f"EXI decoding returned null for namespace '{namespace}'"
            )
        return str(json_str)

    def decode_to_xml(self, exi_bytes, namespace):
        raise EXIDecodingError(
            "decode_to_xml() is no longer supported by the EXIficient backend. "
            "Use decode() instead (returns JSON)."
        )

    def get_version(self):
        self._ensure_started()
        return str(self._codec.get_version())

    @property
    def jar_path(self):
        self._ensure_started()
        return self._jar_path or "(unknown)"


_default_handler = None


def get_handler():
    global _default_handler
    if _default_handler is None:
        _default_handler = ExiCodecHandler()
    return _default_handler