"""本地诊断日志：~/.szu-agent/debug.log（最多 2MB × 2 个文件）。

只记录排查需要的结构信息：请求的主机和路径、状态码、跳转链、学校返回的结果码、Cookie 的名字。
绝不记录：密码、验证码、Cookie 的值、学号姓名、查询参数（里面可能有 ticket）。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from .session import agent_home

_logger = logging.getLogger("szu_agent.campus")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_path = None


def log() -> logging.Logger:
    """日志文件跟随 SZU_AGENT_HOME：数据目录变了（例如测试用临时目录）就换文件。"""
    global _path
    path = agent_home() / "debug.log"
    if path != _path:
        for h in list(_logger.handlers):
            _logger.removeHandler(h)
            h.close()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=1, encoding="utf-8")
            h.setFormatter(logging.Formatter(f"%(asctime)s [pid {os.getpid()}] %(message)s"))
            _logger.addHandler(h)
        except OSError:
            _logger.addHandler(logging.NullHandler())
        _path = path
    return _logger


def where(url: object) -> str:
    """只保留主机和路径，丢掉查询参数（可能含 ticket）。"""
    u = urlparse(str(url))
    return f"{u.hostname}{u.path}"
