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
import tempfile
import threading
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
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
#   - Web UI 保存的配置持久化到 data/bark_config.json，优先级高于环境变量；
#     文件不存在或读取失败时自动回退到环境变量，保证旧部署方式兼容。
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

# 由 app.py 启动时注入（data/bark_config.json）；未注入时仅使用环境变量
_config_file = None


def set_config_file(path: str) -> None:
    """设置持久化配置文件路径。由 app.py 在启动时调用。"""
    global _config_file
    _config_file = str(path) if path else None


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


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "on", "yes", "enabled")


@dataclass(frozen=True)
class BarkConfig:
    """Bark 配置快照（业务代码无需关心配置来自环境变量还是 Web UI）。"""

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

    @classmethod
    def from_mapping(cls, data, base: "BarkConfig | None" = None) -> "BarkConfig":
        """从 Web UI/JSON 字段构造配置；缺失字段沿用 base（默认环境变量）。"""
        base = base or cls.from_env()
        if not isinstance(data, dict):
            return base

        def text(name: str, current: str) -> str:
            if name not in data:
                return current
            value = data.get(name)
            return "" if value is None else str(value).strip()

        def flag(names, current: bool) -> bool:
            for name in names:
                if name in data:
                    return _as_bool(data.get(name), current)
            return current

        def number(name: str, current: float) -> float:
            if name not in data:
                return current
            try:
                parsed = float(data.get(name))
            except (TypeError, ValueError):
                return current
            return parsed if parsed > 0 else current

        server = text("server", base.server).rstrip("/") or DEFAULT_SERVER
        return cls(
            enable=flag(("enabled", "enable"), base.enable),
            key=text("key", base.key),
            server=server,
            icon=text("icon", base.icon),
            group=text("group", base.group),
            sound_success=text("sound_success", base.sound_success),
            sound_error=text("sound_error", base.sound_error),
            sound_info=text("sound_info", base.sound_info),
            timeout=number("timeout", base.timeout),
        )

    def to_dict(self) -> dict:
        """转为 Web UI / 持久化文件使用的字段（不含任何环境变量信息）。"""
        return {
            "enabled": self.enable,
            "key": self.key,
            "server": self.server,
            "icon": self.icon,
            "group": self.group,
            "sound_success": self.sound_success,
            "sound_error": self.sound_error,
            "sound_info": self.sound_info,
            "timeout": self.timeout,
        }


def load_config() -> BarkConfig:
    """
    读取当前配置：环境变量作为兜底，data/bark_config.json 覆盖同名字段。

    每次推送重新读取，Web UI 保存后无需重启即可生效。
    """
    config = BarkConfig.from_env()
    path = _config_file
    if not path or not os.path.isfile(path):
        return config
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("[bark] 读取配置文件失败 %s: %s", path, exc)
        return config
    if not isinstance(data, dict):
        logger.warning("[bark] 配置文件格式错误，已回退环境变量: %s", path)
        return config
    return BarkConfig.from_mapping(data, config)


def save_config(config: BarkConfig) -> None:
    """原子写入配置文件，避免备份/断电时产生半截 JSON。"""
    if not _config_file:
        raise RuntimeError("未设置 Bark 配置文件路径")
    parent = os.path.dirname(os.path.abspath(_config_file))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".bark_config.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, _config_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("[bark] 配置已保存: %s", _config_file)


def validate_config(config: BarkConfig) -> str:
    """校验配置，返回错误信息；空字符串表示通过。"""
    if config.enable and not config.key:
        return "启用推送时需要填写 Bark 设备 Key"
    for label, value in (("Bark 服务地址", config.server), ("图标地址", config.icon)):
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return f"{label}必须是完整的 http(s) URL"
    if config.timeout < 1 or config.timeout > 60:
        return "超时时间需在 1-60 秒之间"
    return ""


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


def _send_with_config(
    config: BarkConfig,
    level: str,
    title: str,
    content: str,
    icon: str = "",
    jump_url: str = "",
    group: str = "",
    mute: bool = False,
) -> bool:
    """使用给定配置发送一次请求（同步，供 notify / 测试接口复用）。"""
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
        icon:     推送图标 URL，为空时使用已保存配置
        jump_url: 点击通知跳转的 URL
        group:    通知分组，为空时使用已保存配置
        mute:     是否静音；为 True 时强制 sound=none 且 level=passive

    返回是否推送成功。任何异常都会被捕获并记录日志，不会向调用方抛出。
    """
    config = load_config()
    if not config.enable:
        return False
    if not config.key:
        logger.warning("[bark] 推送已启用但未配置设备 Key，跳过推送")
        return False
    return _send_with_config(config, level, title, content, icon, jump_url, group, mute)


def send_test(
    config: BarkConfig,
    title: str = "Bark 推送测试",
    content: str = "如果你收到这条消息，说明 Bark 推送配置正确。",
) -> bool:
    """使用指定配置发送测试推送，不受“启用”开关限制（便于保存前测试）。"""
    if not config.key:
        logger.warning("[bark] 测试推送失败：未配置设备 Key")
        return False
    return _send_with_config(config, LEVEL_SUCCESS, title, content)


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
