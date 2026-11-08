"""JSON 로깅. 로그에 실수로 섞인 개인정보는 기록 직전에 마스킹한다.

CloudWatch Logs 도 개인정보처리시스템의 일부가 될 수 있다. 애플리케이션이 조심하는 것과 별개로
마지막 방어선을 둔다.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from .pii.detect import mask


class PiiScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(record.getMessage())
        record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(PiiScrubFilter())
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    return logger
