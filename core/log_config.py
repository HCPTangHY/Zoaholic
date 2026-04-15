import logging
import logging.handlers
import os
import queue
import re
import sys
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Optional

DEFAULT_BACKEND_LOG_PAGE_SIZE = max(1, int(os.getenv("BACKEND_LOG_PAGE_SIZE", "200")))
DEFAULT_BACKEND_LOG_BUFFER_SIZE = max(50, int(os.getenv("BACKEND_LOG_BUFFER_SIZE", "200")))
MAX_BACKEND_LOG_PAGE_SIZE = 2000
MAX_BACKEND_LOG_BUFFER_SIZE = 50000
BACKEND_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
STREAM_DEFAULT_LEVELS = {
    "stdout": "INFO",
    "stderr": "ERROR",
}
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

_BACKEND_LOG_BUFFER_SIZE = DEFAULT_BACKEND_LOG_BUFFER_SIZE
_backend_log_buffer: deque[Dict[str, Any]] = deque(maxlen=_BACKEND_LOG_BUFFER_SIZE)
_backend_log_lock = RLock()
_backend_log_next_id = 1
_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr
_log_listener = None
_LOGGER_LINE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}.*? - (?P<logger>.*?) - (?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL) - (?P<message>.*)$"
)
_PREFIX_LEVEL_PATTERN = re.compile(
    r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):\s+(?P<message>.*)$"
)


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalize_level_name(level: Any) -> Optional[str]:
    if level is None:
        return None

    text = str(level).strip().upper()
    if text == "WARN":
        text = "WARNING"
    return text if text in BACKEND_LOG_LEVELS else None


def _extract_log_metadata(text: str) -> Dict[str, Optional[str]]:
    normalized_text = str(text or "")

    match = _LOGGER_LINE_PATTERN.match(normalized_text)
    if match:
        return {
            "level": match.group("level"),
            "logger_name": (match.group("logger") or "").strip() or None,
            "message": match.group("message"),
        }

    match = _PREFIX_LEVEL_PATTERN.match(normalized_text)
    if match:
        return {
            "level": match.group("level"),
            "logger_name": None,
            "message": match.group("message"),
        }

    return {
        "level": None,
        "logger_name": None,
        "message": None,
    }


class TeeStream:
    """将 stdout/stderr 同时写入原始流与内存缓冲区。"""

    def __init__(self, original_stream, stream_name: str):
        self.original_stream = original_stream
        self.stream_name = stream_name
        self._pending = ""

    def write(self, data):
        if data is None:
            return 0
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = str(data)

        written = self.original_stream.write(text)
        self._capture(text)
        return written if written is not None else len(text)

    def flush(self):
        self._flush_pending()
        return self.original_stream.flush()

    def isatty(self):
        return getattr(self.original_stream, "isatty", lambda: False)()

    def fileno(self):
        return self.original_stream.fileno()

    def _capture(self, text: str):
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._pending += normalized

        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            _append_backend_log_line(line, self.stream_name, source="stream")

    def _flush_pending(self):
        if self._pending:
            _append_backend_log_line(self._pending, self.stream_name, source="stream")
            self._pending = ""

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


class MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int):
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


class BackendCaptureStreamHandler(logging.StreamHandler):
    def __init__(self, original_stream, stream_name: str):
        super().__init__(original_stream)
        self.stream_name = stream_name

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        _append_backend_log_line(
            _render_log_message_for_buffer(record, self.formatter),
            self.stream_name,
            level=record.levelname,
            logger_name=record.name,
            source="logger",
            captured_at=datetime.fromtimestamp(record.created, timezone.utc),
        )


def _render_log_message_for_buffer(record: logging.LogRecord, formatter: Optional[logging.Formatter]) -> str:
    message = record.getMessage()
    active_formatter = formatter or logging.Formatter()

    if record.exc_info:
        exc_text = active_formatter.formatException(record.exc_info)
        if exc_text:
            message = f"{message}\n{exc_text}"

    if record.stack_info:
        stack_text = active_formatter.formatStack(record.stack_info)
        if stack_text:
            message = f"{message}\n{stack_text}"

    return message


def _append_backend_log_line(
    message: str,
    stream: str,
    *,
    level: Optional[str] = None,
    logger_name: Optional[str] = None,
    source: str = "stream",
    captured_at: Optional[datetime] = None,
):
    raw_text = str(message or "")
    if not raw_text.strip():
        return

    normalized_stream = "stderr" if str(stream).strip().lower() == "stderr" else "stdout"
    metadata = _extract_log_metadata(raw_text)
    normalized_level = (
        _normalize_level_name(level)
        or _normalize_level_name(metadata.get("level"))
        or STREAM_DEFAULT_LEVELS.get(normalized_stream)
    )
    normalized_logger_name = (str(logger_name).strip() if logger_name else "") or metadata.get("logger_name")
    normalized_message = (metadata.get("message") or raw_text).strip()
    normalized_source = source if source in {"stream", "logger"} else "stream"

    global _backend_log_next_id
    entry = {
        "id": _backend_log_next_id,
        "captured_at": captured_at if isinstance(captured_at, datetime) else datetime.now(timezone.utc),
        "stream": normalized_stream,
        "level": normalized_level,
        "logger_name": normalized_logger_name or None,
        "source": normalized_source,
        "message": normalized_message,
    }

    with _backend_log_lock:
        _backend_log_buffer.append(entry)
        _backend_log_next_id += 1


def _install_backend_log_capture():
    if getattr(sys, "_zoaholic_backend_log_capture_installed", False):
        return

    if not isinstance(sys.stdout, TeeStream):
        sys.stdout = TeeStream(sys.stdout, "stdout")
    if not isinstance(sys.stderr, TeeStream):
        sys.stderr = TeeStream(sys.stderr, "stderr")

    sys._zoaholic_backend_log_capture_installed = True


def _configure_root_logging():
    global _log_listener
    if getattr(logging, "_zoaholic_root_logging_configured", False):
        return

    stdout_handler = BackendCaptureStreamHandler(_ORIGINAL_STDOUT, "stdout")
    stdout_handler.addFilter(MaxLevelFilter(logging.INFO))
    stdout_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    stderr_handler = BackendCaptureStreamHandler(_ORIGINAL_STDERR, "stderr")
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    # Use QueueHandler + QueueListener so that logger.xxx() calls never
    # block the event loop on stream.write() — the actual I/O happens in
    # a dedicated background thread managed by QueueListener.
    log_queue = queue.Queue(-1)  # unbounded
    queue_handler = logging.handlers.QueueHandler(log_queue)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[queue_handler],
        force=True,
    )

    _log_listener = logging.handlers.QueueListener(
        log_queue, stdout_handler, stderr_handler,
        respect_handler_level=True,
    )
    _log_listener.start()

    logging._zoaholic_root_logging_configured = True


_install_backend_log_capture()
_configure_root_logging()

logger = logging.getLogger("Zoaholic")

logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("watchfiles.main").setLevel(logging.CRITICAL)


# ==================== 后台日志查询/配置接口 ====================


def get_backend_log_settings(preferences: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    prefs = preferences if isinstance(preferences, dict) else {}
    page_size = _coerce_int(
        prefs.get("backend_logs_page_size"),
        DEFAULT_BACKEND_LOG_PAGE_SIZE,
        1,
        MAX_BACKEND_LOG_PAGE_SIZE,
    )
    buffer_size = _coerce_int(
        prefs.get("backend_log_buffer_size"),
        _BACKEND_LOG_BUFFER_SIZE,
        50,
        MAX_BACKEND_LOG_BUFFER_SIZE,
    )

    return {
        "page_size": page_size,
        "buffer_size": buffer_size,
    }


def set_backend_log_buffer_size(size: int) -> int:
    normalized_size = _coerce_int(size, DEFAULT_BACKEND_LOG_BUFFER_SIZE, 50, MAX_BACKEND_LOG_BUFFER_SIZE)

    global _BACKEND_LOG_BUFFER_SIZE, _backend_log_buffer
    with _backend_log_lock:
        snapshot = list(_backend_log_buffer)[-normalized_size:]
        _BACKEND_LOG_BUFFER_SIZE = normalized_size
        _backend_log_buffer = deque(snapshot, maxlen=normalized_size)

    return normalized_size


def apply_backend_log_preferences(preferences: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    settings = get_backend_log_settings(preferences)
    settings["buffer_size"] = set_backend_log_buffer_size(settings["buffer_size"])
    return settings


def get_backend_log_entries(
    *,
    since_id: Optional[int] = None,
    limit: int = DEFAULT_BACKEND_LOG_PAGE_SIZE,
    search: Optional[str] = None,
    stream: Optional[str] = None,
    level: Optional[str] = None,
    level_group: Optional[str] = None,
    logger_name: Optional[str] = None,
) -> Dict[str, Any]:
    """返回当前进程最近的后台日志快照。"""

    normalized_stream = (stream or "").strip().lower() or None
    normalized_level = _normalize_level_name(level)
    normalized_search = (search or "").strip().lower()
    normalized_logger_name = (logger_name or "").strip().lower() or None
    normalized_level_group = (level_group or "").strip().lower() or None
    allowed_levels = None
    if normalized_level_group == "errors":
        allowed_levels = {"ERROR", "CRITICAL"}

    with _backend_log_lock:
        snapshot: List[Dict[str, Any]] = list(_backend_log_buffer)
        max_id = _backend_log_next_id - 1

    filtered: List[Dict[str, Any]] = []
    for entry in snapshot:
        if since_id is not None and entry["id"] <= since_id:
            continue
        if normalized_stream and entry["stream"] != normalized_stream:
            continue
        if normalized_level and entry.get("level") != normalized_level:
            continue
        if allowed_levels is not None and entry.get("level") not in allowed_levels:
            continue
        entry_logger_name = str(entry.get("logger_name") or "").strip().lower()
        if normalized_logger_name and entry_logger_name != normalized_logger_name:
            continue
        if normalized_search and normalized_search not in entry["message"].lower():
            continue
        filtered.append(entry)

    total = len(filtered)
    if limit > 0:
        if since_id is not None:
            filtered = filtered[:limit]
        else:
            filtered = filtered[-limit:]

    return {
        "items": filtered,
        "total": total,
        "max_id": max_id,
        "buffer_size": _BACKEND_LOG_BUFFER_SIZE,
    }


# ==================== 出站请求拦截安装 ====================
# 在所有模块之前完成 httpx monkey-patch，确保每个 AsyncClient 实例自动记录出站请求
try:
    from core.http import install as _install_http_trace
    _install_http_trace()
except Exception:
    pass
