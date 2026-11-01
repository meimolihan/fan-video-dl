#!/usr/bin/env python3
"""
fan-video-dl - Bark 推送通知模块
将 Bark（iOS 推送）能力从下载业务中独立抽离，仅依赖 Python 标准库。
配置全部通过环境变量读取，BARK_ENABLE=false（默认）时所有推送调用立即返回，
不会发起任何网络请求，也不会影响原有下载逻辑。

开源地址: https://github.com/meimolihan/fan-video-dl
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# ============================================================================
# Bark 配置区域（独立抽离，全部通过环境变量配置）
#
#	BARK_ENABLE         总开关。true/1/on/yes 启用，其余（含未设置）关闭。
#	                    默认：false
#	BARK_KEY            Bark 设备 Key（必填）。在 Bark App 首页复制。
#	BARK_SERVER         Bark 服务地址，支持自建服务器。
#	                    默认：https://api.day.app
#	BARK_ICON           默认推送图标 URL（可选，iOS 15+ 生效）。
#	BARK_GROUP          默认通知分组（可选，建议不超过 8 个字符）。
#	BARK_SOUND_SUCCESS  成功通知铃声。默认：bell
#	BARK_SOUND_ERROR    失败告警铃声。默认：alarm
#	BARK_SOUND_INFO     普通提示铃声。默认：none
#	BARK_TIMEOUT        单次 HTTP 请求超时（秒）。默认：5
#
# 说明：
#   - 中文标题/内容自动做 URL 编码，避免乱码。
#   - 仅发起一次请求，失败只记录日志，不重试、不阻塞调用方。
#   - 异步入口在独立 daemon 线程中执行，调用方立即返回。
#
# 使用示例：
#
#	import bark_notify
#
#	bark_notify.success("视频下载完成", "文件：demo.mp4", jump_url="https://example.com")
#	bark_notify.error("视频下载失败", "原因：网络超时")
#	bark_notify.info("系统提示", "服务将于 10 分钟后重启")
#
#	# 完整参数：级别、标题、内容、图标、跳转URL、分组、是否静音
#	bark_notify.notify_async(
#	    bark_notify.LEVEL_ERROR,
#	    "磁盘告警",
#	    "根分区使用率 95%",
#	    icon="https://example.com/icon.png",
#	    jump_url="https://panel.example.com",
#	    group="fan-video-dl",
#	    mute=False,
#	)
#
# ============================================================================

LEVEL_SUCCESS = "success"
LEVEL_ERROR = "error"
LEVEL_INFO = "info"

DEFAULT_SERVER = "https://api.day.app"
DEFAULT_TIMEOUT = 5.0

_SOUND_SUCCESS = "bell"
_SOUND_ERROR = "alarm"
_SOUND_INFO = "none"


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    value = value.strip()
    return value if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "on", "yes", "enabled")


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class BarkConfig:
    """Bark 配置快照（从环境变量加载，业务代码无需关心配置细节）。"""

    enable: bool
    key: str
    server: str
    icon: str
    group: str
    sound_success: str
    sound_error: str
    sound_info: str
    timeout: float

    @classmethod
    def from_env(cls) -> "BarkConfig":
        server = _env_str("BARK_SERVER", DEFAULT_SERVER).rstrip("/") or DEFAULT_SERVER
        return cls(
            enable=_env_bool("BARK_ENABLE", False),
            key=_env_str("BARK_KEY"),
            server=server,
            icon=_env_str("BARK_ICON"),
            group=_env_str("BARK_GROUP"),
            sound_success=_env_str("BARK_SOUND_SUCCESS", _SOUND_SUCCESS),
            sound_error=_env_str("BARK_SOUND_ERROR", _SOUND_ERROR),
            sound_info=_env_str("BARK_SOUND_INFO", _SOUND_INFO),
            timeout=_env_float("BARK_TIMEOUT", DEFAULT_TIMEOUT),
        )


def load_config() -> BarkConfig:
    """读取当前环境变量。每次推送重新读取，便于运行期调整配置。"""
    return BarkConfig.from_env()


def is_enabled() -> bool:
    """Bark 推送总开关是否打开。"""
    return load_config().enable


def _sound_for_level(config: BarkConfig, level: str) -> str:
    if level == LEVEL_SUCCESS:
        return config.sound_success
    if level == LEVEL_ERROR:
        return config.sound_error
    return config.sound_info


def build_url(
    config: BarkConfig,
    level: str,
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> str:
    """拼接 Bark GET 请求地址，所有参数自动 URL 编码（含中文）。"""
    params = {
        "title": title,
        "body": content,
    }

    icon = icon or config.icon
    group = group or config.group
    if icon:
        params["icon"] = icon
    if jump_url:
        params["url"] = jump_url
    if group:
        params["group"] = group

    if mute:
        # 静音：不响铃，仅进入通知列表
        params["sound"] = "none"
        params["level"] = "passive"
    else:
        sound = _sound_for_level(config, level)
        if sound:
            params["sound"] = sound

    # quote_via=quote 用 %20 表示空格，避免部分服务端把 + 当作字面量
    query = urlencode(params, quote_via=quote, safe="")
    return f"{config.server}/{quote(config.key, safe='')}?{query}"


def notify(
    level: str,
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> bool:
    """
    统一通知函数（同步执行）。

    参数：
        level:    通知级别，LEVEL_SUCCESS / LEVEL_ERROR / LEVEL_INFO，决定铃声
        title:    推送标题
        content:  推送内容（中文自动 URL 编码）
        icon:     推送图标 URL，为空时使用 BARK_ICON
        jump_url: 点击通知跳转的 URL
        group:    通知分组，为空时使用 BARK_GROUP
        mute:     是否静音；为 True 时强制 sound=none 且 level=passive

    返回是否推送成功。任何异常都会被捕获并记录日志，不会向调用方抛出。
    """
    config = load_config()
    if not config.enable:
        return False
    if not config.key:
        logger.warning("[bark] BARK_ENABLE=true 但未配置 BARK_KEY，跳过推送")
        return False

    try:
        target = build_url(config, level, title, content, icon, jump_url, group, mute)
        request = Request(
            target,
            method="GET",
            headers={
                "User-Agent": "fan-video-dl/bark",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=config.timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(4096)

        if status < 200 or status >= 300:
            logger.warning("[bark] 推送失败 level=%s title=%r: HTTP %s", level, title, status)
            return False

        if raw:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code")
                if code is not None:
                    try:
                        code_ok = int(code) in (0, 200)
                    except (TypeError, ValueError):
                        code_ok = True
                    if not code_ok:
                        logger.warning(
                            "[bark] 推送失败 level=%s title=%r: code=%s message=%s",
                            level,
                            title,
                            code,
                            payload.get("message", ""),
                        )
                        return False
        return True
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1024).decode("utf-8", "replace").strip()
        except Exception:
            pass
        logger.warning("[bark] 推送失败 level=%s title=%r: HTTP %s %s", level, title, exc.code, detail)
        return False
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("[bark] 推送失败 level=%s title=%r: %s", level, title, exc)
        return False
    except Exception as exc:  # 兜底：推送绝不影响主流程
        logger.warning("[bark] 推送失败 level=%s title=%r: %s", level, title, exc)
        return False


def notify_async(
    level: str,
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> None:
    """统一通知函数的异步入口，立即返回，不阻塞业务线程。"""
    if not is_enabled():
        return
    try:
        thread = threading.Thread(
            target=notify,
            args=(level, title, content, icon, jump_url, group, mute),
            name="bark-notify",
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        logger.warning("[bark] 创建推送线程失败: %s", exc)


def success(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> bool:
    """发送成功通知（使用 BARK_SOUND_SUCCESS 铃声）。"""
    return notify(LEVEL_SUCCESS, title, content, icon, jump_url, group, mute)


def error(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> bool:
    """发送失败告警通知（使用 BARK_SOUND_ERROR 铃声）。"""
    return notify(LEVEL_ERROR, title, content, icon, jump_url, group, mute)


def info(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> bool:
    """发送普通提示通知（使用 BARK_SOUND_INFO 铃声）。"""
    return notify(LEVEL_INFO, title, content, icon, jump_url, group, mute)


def success_async(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> None:
    notify_async(LEVEL_SUCCESS, title, content, icon, jump_url, group, mute)


def error_async(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> None:
    notify_async(LEVEL_ERROR, title, content, icon, jump_url, group, mute)


def info_async(
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> None:
    notify_async(LEVEL_INFO, title, content, icon, jump_url, group, mute)
