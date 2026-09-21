#!/usr/bin/env python3
"""
fan-video-dl - Web UI for yt-dlp
基于 yt-dlp 的视频下载器，支持 TLS 指纹伪装（绕过基础 Cloudflare 拦截）、多线程下载、实时进度
开源地址: https://github.com/meimolihan/fan-video-dl
"""

import os
import sys
import json
import logging
import re
import uuid
import shutil
import hashlib
import hmac
import secrets
import sqlite3
import threading
import subprocess
import time
import mimetypes
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_file, render_template, session
from pathlib import Path

import douyin_downloader

# ─── 版本号 ───
# 优先读环境变量 APP_VERSION(CI构建时注入), 其次读取仓库根 version.txt(发版脚本维护), 最后本地开发用日期版本
def _get_version():
    """获取版本号: APP_VERSION(CI) > version.txt > 本地日期版本"""
    v = os.environ.get('APP_VERSION')
    if v:
        return v
    ver_file = Path(__file__).resolve().parent / 'version.txt'
    if ver_file.exists():
        v = ver_file.read_text(encoding='utf-8').strip()
        if v:
            return v
    # 本地开发环境: 用 git describe 或日期
    return f'v{(datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d")}-dev'

VERSION = _get_version()

# ─── 日志 ───
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "data" / "users.db"
DB_PATH.parent.mkdir(exist_ok=True)

# 代理配置持久化到 data/proxy_config.json, 由 douyin_downloader 托管读写
douyin_downloader.set_proxy_file(str(DB_PATH.parent / 'proxy_config.json'))


def _load_or_create_secret_key():
    """SECRET_KEY: 优先环境变量(忽略已知占位值); 否则持久化到 data/secret_key, 避免重启后 session 全部失效"""
    env_key = os.environ.get('SECRET_KEY')
    if env_key and env_key != 'change-me-to-a-random-string':
        return env_key
    key_file = DB_PATH.parent / 'secret_key'
    if key_file.exists():
        key = key_file.read_text(encoding='utf-8').strip()
        if key:
            return key
    key = secrets.token_hex(32)
    try:
        key_file.write_text(key, encoding='utf-8')
        os.chmod(key_file, 0o600)
        logging.getLogger(__name__).info(f"已生成持久化 SECRET_KEY: {key_file}")
    except OSError as e:
        logging.getLogger(__name__).warning(f"无法持久化 SECRET_KEY: {e}")
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.permanent_session_lifetime = 7 * 24 * 3600  # 7 days

@app.context_processor
def inject_version():
    return {'version': VERSION}

# 存储下载任务状态
tasks = {}
tasks_lock = threading.Lock()

# === 认证相关 ===
MAX_LOGIN_ATTEMPTS = 5
LOCK_TIME = 300  # 5 minutes
login_attempts = {}  # ip -> {count, lock_until}


def init_db():
    """初始化用户数据库"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    ''')
    conn.commit()
    # 检查是否有用户，没有则创建默认用户
    c.execute('SELECT COUNT(*) FROM users')
    if c.fetchone()[0] == 0:
        default_user = os.environ.get('AUTH_USERNAME', 'admin')
        default_pass = os.environ.get('AUTH_PASSWORD', 'admin888')
        create_user(default_user, default_pass)
        logging.getLogger(__name__).warning(
            f"已创建默认用户 '{default_user}'。"
            + ("请立即修改默认密码 'admin888'!" if default_pass == 'admin888' else "")
        )
    conn.close()


PBKDF2_ITERATIONS = 200000


def _hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    """生成 PBKDF2 密码哈希, 格式: pbkdf2$<迭代次数>$<盐>$<hex>"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt.encode('utf-8'), iterations).hex()
    return f'pbkdf2${iterations}${salt}${digest}'


def create_user(username, password):
    """创建用户"""
    salt = secrets.token_hex(16)
    password_hash = _hash_password(password, salt)
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    try:
        c.execute(
            'INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)',
            (username, password_hash, salt, time.time())
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # 用户已存在
    conn.close()


def verify_user(username, password):
    """验证用户名密码 (兼容旧版 sha256 哈希, 成功登录后自动升级为 PBKDF2)"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('SELECT password_hash, salt FROM users WHERE username = ?', (username,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    stored_hash, legacy_salt = row

    # 新版 PBKDF2 格式: pbkdf2$<iterations>$<salt>$<hex>
    if stored_hash.startswith('pbkdf2$'):
        try:
            _, iter_str, pb_salt, expected = stored_hash.split('$', 3)
            iterations = int(iter_str)
        except (ValueError, TypeError):
            return False
        actual = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), pb_salt.encode('utf-8'), iterations).hex()
        return hmac.compare_digest(actual, expected)

    # 旧版格式: sha256(salt + password)
    actual = hashlib.sha256((password + legacy_salt).encode()).hexdigest()
    if hmac.compare_digest(actual, stored_hash):
        try:  # 登录成功, 自动升级存储格式
            new_hash = _hash_password(password, legacy_salt)
            conn2 = sqlite3.connect(str(DB_PATH))
            conn2.execute('UPDATE users SET password_hash=? WHERE username=?',
                          (new_hash, username))
            conn2.commit()
            conn2.close()
        except sqlite3.Error:
            pass
        return True
    return False


def change_password(username, old_password, new_password):
    """修改密码"""
    if not verify_user(username, old_password):
        return False
    salt = secrets.token_hex(16)
    password_hash = _hash_password(new_password, salt)
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute('UPDATE users SET password_hash = ?, salt = ? WHERE username = ?',
              (password_hash, salt, username))
    conn.commit()
    conn.close()
    return True


def change_username(old_username, new_username, password):
    """修改用户名"""
    if not verify_user(old_username, password):
        return False
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    try:
        c.execute('UPDATE users SET username = ? WHERE username = ?', (new_username, old_username))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def get_client_ip():
    """获取客户端 IP"""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or 'unknown'


def check_login_lock(ip):
    """检查 IP 是否被锁定"""
    info = login_attempts.get(ip)
    if not info:
        return False
    if info.get('lock_until') and time.time() < info['lock_until']:
        return True
    if info.get('lock_until') and time.time() >= info['lock_until']:
        del login_attempts[ip]
    return False


def record_failed_login(ip):
    """记录失败登录"""
    info = login_attempts.setdefault(ip, {'count': 0, 'lock_until': None})
    info['count'] += 1
    if info['count'] >= MAX_LOGIN_ATTEMPTS:
        info['lock_until'] = time.time() + LOCK_TIME


def clear_login_attempts(ip):
    """清除登录尝试记录"""
    login_attempts.pop(ip, None)


def login_required(f):
    """认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': '未登录', 'auth_required': True}), 401
        return f(*args, **kwargs)
    return decorated_function

# 初始化数据库
init_db()


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()[:200]


def get_file_size_str(size_bytes):
    """将字节数转为人类可读格式"""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def extract_real_video_url(url):
    """从网页中智能提取真实视频地址(m3u8/mp4), 支持 MacCMS 等播放器"""
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, impersonate='chrome', timeout=30,
                              proxies=douyin_downloader.get_proxies())
        if r.status_code != 200:
            return None
        html = r.text

        # 0. 先找 iframe player (如 /Player/V2?payload=xxx), 请求它获取带token的m3u8
        iframe_matches = re.findall(r'src=["\']([^"\']*/Player/[^"\']+)', html, re.I)
        if iframe_matches:
            from urllib.parse import urljoin
            for iframe_src in iframe_matches:
                iframe_url = urljoin(url, iframe_src)
                try:
                    r2 = cffi_requests.get(iframe_url, impersonate='chrome', timeout=20,
                                           headers={'Referer': url},
                                           proxies=douyin_downloader.get_proxies())
                    if r2.status_code == 200:
                        # 在iframe页面里找带token的m3u8 (优先返回有token的)
                        token_m3u8 = re.findall(r'["\']([^"\' ]*m3u8[^"\' ]*token=[^"\' ]*)["\']', r2.text)
                        if token_m3u8:
                            u = token_m3u8[0].replace(chr(92)+chr(47), '/')
                            if u.startswith('http'):
                                return u
                        # 有token的没找到, 找普通的m3u8
                        iframe_m3u8 = re.findall(r'["\']([^"\' ]*m3u8[^"\' ]*)["\']', r2.text)
                        for u in iframe_m3u8:
                            u = u.replace(chr(92)+chr(47), '/')
                            if u.startswith('http'):
                                return u
                except Exception:
                    pass

        # 1. MacCMS player_aaaa 变量中的 m3u8/url
        m = re.search(r'player_aaaa\s*=\s*(\{[^}]+\})', html)
        if m:
            import json as _json
            try:
                pdata = _json.loads(m.group(1))
                vurl = pdata.get('url', '')
                if vurl and ('m3u8' in vurl or '.mp4' in vurl):
                    return vurl.replace(chr(92)+chr(47), '/')
            except Exception:
                pass

        # 2. 直接搜索 m3u8 链接 (优先找带token参数的)
        m3u8_matches = re.findall(r'["\']([^"\' ]*m3u8[^"\' ]*token=[^"\' ]*)["\']', html)
        for u in m3u8_matches:
            u = u.replace(chr(92)+chr(47), '/')
            if u.startswith('http'):
                return u
        # 再找普通m3u8
        m3u8_matches = re.findall(r'["\']([^"\' ]*m3u8[^"\' ]*)["\']', html)
        for u in m3u8_matches:
            u = u.replace(chr(92)+chr(47), '/')
            if u.startswith('http'):
                return u

        # 3. 搜索 mp4 直链
        mp4_matches = re.findall(r'[\'\"\'](https?://[^\'\"\' ]+\.mp4[^\'\"\' ]*)[\'\"\']', html)
        for u in mp4_matches:
            u = u.replace(chr(92)+chr(47), '/')
            return u

        # 4. 搜索 video 标签 src
        video_src = re.findall(r'<video[^>]*src=[\'\"\']([^\'\"\' ]+)', html, re.I)
        for u in video_src:
            if u.startswith('http'):
                return u

        # 5. 检查页面是否是 JS 安全验证(内容太短)
        if len(html) < 3000:
            return '__JS_CHALLENGE__'

    except Exception:
        pass
    return None


def is_tiktok_url(url):
    """判断是否为 TikTok 链接"""
    return 'tiktok.com' in url or 'douyin.com' in url


def is_douyin_url(url):
    """判断是否为抖音视频链接"""
    return douyin_downloader.is_douyin_url(url)


def _get_ytdlp_invocation():
    """解析 yt-dlp 启动命令 (返回 argv 列表):
    1) YTDLP_BIN 环境变量显式指定
    2) 系统 PATH (shutil.which)
    3) 回退: 当前 Python venv 同目录 (install.sh 部署时 yt-dlp 装在 venv 里, 而 systemd 的 PATH 不含 venv)
    4) 再回退: 若是 Python 包已安装但缺少脚本入口 (pip 安装异常等), 用 `python -m yt_dlp`
    全部失败返回 None
    """
    override = os.environ.get('YTDLP_BIN', '').strip()
    if override:
        if os.path.isfile(override):
            return [override]
        logging.getLogger(__name__).warning(f"YTDLP_BIN 指定的文件不存在: {override}")
    found = shutil.which('yt-dlp')
    if found:
        return [found]
    venv_bin = Path(sys.executable).resolve().parent / 'yt-dlp'
    if venv_bin.is_file():
        return [str(venv_bin)]
    try:
        import importlib.util
        if importlib.util.find_spec('yt_dlp') is not None:
            return [sys.executable, '-m', 'yt_dlp']
    except Exception:
        pass
    return None


# 启动时解析一次并记录日志, 便于排查"yt-dlp 未安装"
YTDLP_INV = _get_ytdlp_invocation()
if YTDLP_INV:
    logging.getLogger(__name__).info("yt-dlp 定位成功: %s", ' '.join(YTDLP_INV))
else:
    logging.getLogger(__name__).warning("未检测到 yt-dlp, 请安装后重启服务")


def build_ytdlp_cmd(url, options, output_template=None):
    """构建 yt-dlp 命令行"""
    fmt = options.get('format', 'best')
    if output_template is None:
        # 文件名含视频ID+画质标签+格式后缀, 避免不同画质/格式互相覆盖或被yt-dlp跳过
        if fmt == 'audio':
            ext, tag = 'mp3', 'MP3'
        elif fmt == '1080p':
            ext, tag = 'mp4', '1080p'
        elif fmt == '720p':
            ext, tag = 'mp4', '720p'
        else:
            ext, tag = 'mp4', 'BEST'
        output_template = str(DOWNLOAD_DIR / f'%(title)s [%(id)s][{tag}].{ext}')

    cmd = [
        *(YTDLP_INV or ['yt-dlp']),
        '--no-check-certificates',
        '--extractor-args', 'generic:impersonate',
        '--newline',
        '--enable-file-urls',
        '--progress',
        '--continue',
        '--no-overwrites',
        '--retries', '10',
        '--fragment-retries', '10',
        '--progress-template', 'P|%(progress._percent_str)s|%(progress._total_bytes_estimate_str)s|%(progress._speed_str)s|%(progress._eta_str)s|%(progress.fragment_index)s|%(progress.fragment_count)s',
        '-o', output_template,
    ]

    # 格式选择 — 优先选最佳视频+音频合并，回退到预合并单文件
    if fmt == 'audio':
        cmd.extend(['-x', '--audio-format', 'mp3', '--audio-quality', '0'])
    elif fmt == '720p':
        cmd.extend(['-f', 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'])
    elif fmt == '1080p':
        cmd.extend(['-f', 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'])
    else:
        cmd.extend(['-f', 'bestvideo+bestaudio/best'])

    # 并发下载
    concurrent = int(options.get('concurrent', 10))
    cmd.extend(['--concurrent-fragments', str(concurrent)])
    cmd.extend(['--throttled-rate', '100K'])

    # 合并输出优先 mp4 容器; 编码兼容性由下载后的 ensure_browser_playable() 兜底处理
    if fmt != 'audio' and shutil.which('ffmpeg'):
        cmd.extend(['--merge-output-format', 'mp4'])

    # 代理: UI 配置了代理时传递给 yt-dlp
    proxy_cfg = douyin_downloader.get_proxy_config()
    if proxy_cfg['enabled'] and proxy_cfg['url']:
        cmd.extend(['--proxy', proxy_cfg['url']])

    cmd.append(url)
    return cmd


def ensure_browser_playable(filename, task=None):
    """确保输出文件为浏览器可直接播放的 H.264/AAC MP4。

    对 AV1/VP9/HEVC 等编码或非 MP4 容器自动用 ffmpeg 转码, 已是 H.264 MP4 则跳过。
    就地替换 (必要时改扩展名为 .mp4), 失败时保留原文件。返回最终文件名。
    """
    try:
        path = DOWNLOAD_DIR / filename
        if not path.exists() or path.suffix.lower() not in ('.mp4', '.mkv', '.webm', '.mov', '.m4v', '.flv', '.ts'):
            return filename
        if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
            return filename

        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=format_name',
             '-show_entries', 'stream=codec_type,codec_name', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=30,
        )
        info = json.loads(probe.stdout or '{}')
        container = (info.get('format', {}) or {}).get('format_name', '')
        vcodec, acodec, has_audio = '', '', False
        for st in info.get('streams', []) or []:
            ctype = st.get('codec_type')
            if ctype == 'video' and not vcodec:
                vcodec = (st.get('codec_name') or '').lower()
            elif ctype == 'audio':
                acodec = acodec or (st.get('codec_name') or '').lower()
                has_audio = True
        is_mp4 = ('mp4' in container) or ('mov' in container)
        if vcodec == 'h264' and is_mp4 and (not has_audio or acodec == 'aac'):
            return filename

        if task is not None:
            with tasks_lock:
                task['status'] = 'merging'
                task['percent'] = 100.0

        # 预估时长用于超时保护, 避免超大视频/异常文件无限卡在合并中
        dur = 0.0
        try:
            dprobe = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'csv=p=0', str(path)], capture_output=True, text=True, timeout=20)
            if dprobe.stdout.strip():
                dur = float(dprobe.stdout.strip().splitlines()[0])
        except Exception:
            pass
        timeout_s = max(600, int(dur) * 3 + 600)

        tmp = path.with_name(path.stem + '.transcode.tmp.mp4')
        args = ['ffmpeg', '-nostdin', '-y', '-i', str(path), '-map', '0:v:0']
        if has_audio:
            args += ['-map', '0:a:0?']
        args += ['-c:v', 'libx264', '-crf', '20', '-preset', 'veryfast', '-pix_fmt', 'yuv420p']
        if has_audio:
            args += ['-c:a', 'aac', '-b:a', '192k']
        args += ['-movflags', '+faststart', str(tmp)]
        logging.getLogger(__name__).info(
            f'开始转码 {path.name} (源编码 {vcodec}, 时长 {dur:.0f}s, 超时上限 {timeout_s}s) -> H.264/AAC MP4')
        t0 = time.time()
        try:
            run = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            logging.getLogger(__name__).warning(f'转码超时(>{timeout_s}s), 保留原文件 {path.name}')
            return filename
        elapsed = time.time() - t0
        if run.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            path.unlink()
            newpath = path.with_suffix('.mp4')
            tmp.replace(newpath)
            logging.getLogger(__name__).info(
                f'转码完成 {newpath.name} (源编码 {vcodec}, 耗时 {elapsed:.0f}s)')
            return newpath.name
        if tmp.exists():
            tmp.unlink()
        logging.getLogger(__name__).warning(
            f'转码失败, 保留原文件 {filename}: {(run.stderr or "")[-300:]}')
        return filename
    except Exception as e:
        logging.getLogger(__name__).warning(f'转码检查异常: {e}')
        return filename


def run_douyin_download(task_id, url, options):
    """抖音专用下载: 通过 API 获取无水印视频地址, 直接下载"""
    task = tasks[task_id]
    with tasks_lock:
        task['status'] = 'downloading'
        task['percent'] = 0
        task['error'] = ''
        task['started_at'] = time.time()

    # 解析短链接 (v.douyin.com)
    resolved_url = douyin_downloader.resolve_short_url(url)

    # 提取视频ID
    aweme_id = douyin_downloader.extract_aweme_id(resolved_url)
    if not aweme_id:
        with tasks_lock:
            task['status'] = 'failed'
            task['error'] = '无法提取抖音视频ID, 请检查链接'
        return

    # 获取视频信息
    info = douyin_downloader.get_video_info(aweme_id)
    if not info['ok']:
        with tasks_lock:
            task['status'] = 'failed'
            task['error'] = info.get('error', '获取视频信息失败')
        return

    # 构建文件名 (含aweme_id避免同名/重复下载覆盖)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', info['title']).strip()[:150]
    filename = f"{title} [{aweme_id}].mp4"
    output_path = DOWNLOAD_DIR / filename

    with tasks_lock:
        task['output_file'] = filename
        task['total_size'] = f"{info['size']/1024/1024:.1f}MB" if info['size'] else ''

    # 下载视频 (带进度跟踪)
    try:
        from curl_cffi import requests
        r = requests.get(
            info['download_url'],
            headers={
                'User-Agent': douyin_downloader.DOUYIN_UA,
                'Referer': 'https://www.douyin.com/',
                'Cookie': douyin_downloader.get_douyin_cookie(),
            },
            impersonate='chrome',
            timeout=300,
            stream=True,
        )

        if r.status_code != 200:
            with tasks_lock:
                task['status'] = 'failed'
                task['error'] = f'下载失败: HTTP {r.status_code}'
            return

        total = int(r.headers.get('content-length', info['size'] or 0))
        downloaded = 0
        last_update = 0

        with open(output_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_update > 0.5 and total > 0:
                        last_update = now
                        pct = min(downloaded * 100 / total, 99.9)
                        speed = downloaded / (now - task['started_at']) if now > task['started_at'] else 0
                        with tasks_lock:
                            task['percent'] = round(pct, 1)
                            task['speed'] = f"{speed/1024/1024:.1f}MB/s" if speed > 1024*1024 else f"{speed/1024:.0f}KB/s"
                            eta_sec = (total - downloaded) / speed if speed > 0 else 0
                            m, s = divmod(int(eta_sec), 60)
                            task['eta'] = f"{m:02d}:{s:02d}"

        if downloaded == 0:
            with tasks_lock:
                task['status'] = 'failed'
                task['error'] = '下载文件为空'
            if output_path.exists():
                output_path.unlink()
            return

        # ─── 如果选了仅音频(MP3), 用 ffmpeg 提取 ───
        fmt = options.get('format', 'best')
        if fmt == 'audio':
            mp3_path = output_path.with_suffix('.mp3')
            with tasks_lock:
                task['status'] = 'merging'
            try:
                result = subprocess.run(
                    ['ffmpeg', '-y', '-i', str(output_path), '-vn',
                     '-acodec', 'libmp3lame', '-ab', '320k', str(mp3_path)],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0 and mp3_path.exists():
                    output_path.unlink()  # 删除原mp4
                    filename = mp3_path.name
                    with tasks_lock:
                        task['output_file'] = filename
                else:
                    with tasks_lock:
                        task['error'] = f'音频提取失败, 保留视频文件'
            except Exception as ae:
                with tasks_lock:
                    task['error'] = f'音频提取异常: {str(ae)}, 保留视频文件'

        filename = ensure_browser_playable(filename, task)
        with tasks_lock:
            task['status'] = 'completed'
            task['percent'] = 100.0
            task['completed_at'] = time.time()
            task['output_file'] = filename

    except Exception as e:
        with tasks_lock:
            task['status'] = 'failed'
            task['error'] = f'下载异常: {str(e)}'
        if output_path.exists():
            output_path.unlink()


def run_download(task_id, url, options):
    """在后台线程中运行 yt-dlp, 支持 TikTok 重试"""
    task = tasks[task_id]

    # ─── 抖音视频: 使用专用下载器 (不走 yt-dlp) ───
    if is_douyin_url(url):
        run_douyin_download(task_id, url, options)
        return

    # TikTok 链接需要重试机制: TikTok WAF 不稳定, 有时返回不完整页面
    is_tiktok = is_tiktok_url(url)
    max_attempts = 5 if is_tiktok else 1

    if not YTDLP_INV:
        with tasks_lock:
            task['status'] = 'failed'
            task['error'] = ('未检测到 yt-dlp。若为 systemd 部署请执行: '
                             'sudo /var/lib/fan-video-dl/venv/bin/pip install -U yt-dlp; '
                             '或以 root 执行: pip install -U yt-dlp')
        return

    cmd = build_ytdlp_cmd(url, options)
    task['status'] = 'downloading'
    task['started_at'] = time.time()

    # ─── 提前提取真实视频地址 (对于有iframe player的网站可避免第一次yt-dlp失败) ───
    pre_extracted_url = None
    if not is_tiktok:
        pre_extracted_url = extract_real_video_url(url)
        if pre_extracted_url == '__JS_CHALLENGE__':
            pre_extracted_url = None
        elif pre_extracted_url and pre_extracted_url != url:
            # 成功提取到真实地址
            from urllib.parse import urlparse, urlencode, quote
            origin = urlparse(url)
            referer = f"{origin.scheme}://{origin.netloc}/"

            # 如果是带token的m3u8, 需要特殊处理: CDN要求每个子请求都带token
            # 方案: 用 curl_cffi 下载m3u8, 把子playlist/ts路径改写为带token的完整URL,
            # 生成本地proxy m3u8 让 yt-dlp 下载
            if '.m3u8' in pre_extracted_url and 'token=' in pre_extracted_url:
                try:
                    from curl_cffi import requests as cffi_req
                    import tempfile

                    # 解析token参数
                    parsed = urlparse(pre_extracted_url)
                    token_qs = parsed.query  # token=xxx&expires=xxx&token_path=xxx
                    base_path = parsed.path.rsplit('/', 1)[0]  # .../6e0e2c3c-xxx
                    base_url = f"{parsed.scheme}://{parsed.netloc}{base_path}"

                    r = cffi_req.get(pre_extracted_url, impersonate='chrome', timeout=15,
                                     headers={'Referer': referer},
                                     proxies=douyin_downloader.get_proxies())
                    if r.status_code == 200:
                        master_content = r.text

                        # 处理master playlist: 把子playlist路径改成带token的完整URL
                        proxy_lines = []
                        for line in master_content.split('\n'):
                            line = line.strip()
                            if line and not line.startswith('#'):
                                # 这是子playlist路径, 如 720p/video.m3u8
                                sub_url = f"{base_url}/{line}?{token_qs}"
                                # 下载子playlist, 把ts路径也改成带token的
                                r_sub = cffi_req.get(sub_url, impersonate='chrome', timeout=15,
                                                      headers={'Referer': referer},
                                                      proxies=douyin_downloader.get_proxies())
                                if r_sub.status_code == 200:
                                    sub_base = sub_url.rsplit('/', 1)[0]
                                    sub_lines = []
                                    for sline in r_sub.text.split('\n'):
                                        sline = sline.strip()
                                        if sline and not sline.startswith('#'):
                                            # ts路径, 改成带token的完整URL
                                            ts_url = f"{sub_base}/{sline}?{token_qs}"
                                            sub_lines.append(ts_url)
                                        else:
                                            sub_lines.append(sline)
                                    # 写入临时文件
                                    sub_proxy = tempfile.NamedTemporaryFile(
                                        mode='w', suffix='.m3u8', dir=str(DOWNLOAD_DIR),
                                        delete=False)
                                    sub_proxy.write('\n'.join(sub_lines))
                                    sub_proxy.close()
                                    proxy_lines.append(sub_proxy.name)
                                else:
                                    logging.warning(f"子playlist下载失败: {r_sub.status_code}")
                            else:
                                pass

                        if proxy_lines:
                            # 只取第一个(最高画质)的子playlist
                            pre_extracted_url = proxy_lines[0]
                            cmd = [c for c in cmd if c != url]
                            cmd = [c for c in cmd if c != "--extractor-args" and c != "generic:impersonate"]
                            cmd.append("file://" + pre_extracted_url)
                            logging.info(f"创建proxy m3u8: {pre_extracted_url}")
                        else:
                            # master playlist本身就是最低层(直接含ts)
                            proxy_lines = []
                            for line in master_content.split('\n'):
                                line = line.strip()
                                if line and not line.startswith('#'):
                                    ts_url = f"{base_url}/{line}?{token_qs}"
                                    proxy_lines.append(ts_url)
                                else:
                                    proxy_lines.append(line)
                            proxy_file = tempfile.NamedTemporaryFile(
                                mode='w', suffix='.m3u8', dir=str(DOWNLOAD_DIR), delete=False)
                            proxy_file.write('\n'.join(proxy_lines))
                            proxy_file.close()
                            pre_extracted_url = proxy_file.name
                            cmd = [c for c in cmd if c != url]
                            cmd = [c for c in cmd if c != "--extractor-args" and c != "generic:impersonate"]
                            cmd.append("file://" + pre_extracted_url)
                            logging.info(f"创建proxy m3u8(单层): {pre_extracted_url}")
                    else:
                        raise Exception(f"m3u8下载失败: {r.status_code}")
                except Exception as e:
                    logging.warning(f"proxy m3u8创建失败: {e}, 回退到直接URL")
                    cmd = [c for c in cmd if c != url]
                    cmd.extend(['--add-header', f'Referer: {referer}'])
                    cmd.append(pre_extracted_url)
            else:
                # 普通m3u8或mp4, 直接用 + Referer
                cmd = [c for c in cmd if c != url]
                cmd.extend(['--add-header', f'Referer: {referer}'])
                cmd.append(pre_extracted_url)
            logging.info(f"预提取视频地址: {pre_extracted_url[:80]}...")

    download_success = False
    last_error = ''

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            with tasks_lock:
                task['status'] = 'downloading'
                task['percent'] = 0
                task['error'] = f'TikTok 重试中 ({attempt}/{max_attempts})...'
                task['output_file'] = ''
            time.sleep(3)  # 重试间隔

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(DOWNLOAD_DIR),
            )

            task['pid'] = process.pid
            task['process'] = process

            last_update = 0
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue

                # 解析进度模板行: P|  0.1%|   1.84GiB| 300.89KiB/s|91:13:53|1|2084
                if line.startswith('P|'):
                    parts = line.split('|')
                    if len(parts) >= 7:
                        with tasks_lock:
                            pct_str = parts[1].strip().replace('%', '')
                            try:
                                task['percent'] = float(pct_str)
                            except ValueError:
                                pass
                            task['total_size'] = parts[2].strip() if parts[2] != 'NA' else ''
                            task['speed'] = parts[3].strip() if parts[3] != 'NA' else ''
                            task['eta'] = parts[4].strip() if parts[4] != 'NA' else ''
                            frag_cur = parts[5].strip()
                            frag_total = parts[6].strip()
                            if frag_cur and frag_cur != 'NA':
                                try:
                                    task['frag_current'] = int(frag_cur)
                                except ValueError:
                                    pass
                            if frag_total and frag_total != 'NA':
                                try:
                                    task['frag_total'] = int(frag_total)
                                except ValueError:
                                    pass

                # 检测错误
                if line.startswith("ERROR:") or line.startswith("ffmpeg error"):
                    with tasks_lock:
                        task["error"] = line.replace("ERROR:", "").replace("ffmpeg error", "").strip()
                    last_error = task["error"]

                # 检测合并/完成状态
                if '[Merger]' in line or '[ffmpeg]' in line:
                    with tasks_lock:
                        task['status'] = 'merging'
                        task['percent'] = 100.0

                # 检测 [ExtractAudio] 目标文件名 (音频提取: .webm -> .mp3, 原 .webm 会被删除)
                m_ea = re.search(r'\[ExtractAudio\]\s+Destination:\s+(.+)', line)
                if m_ea:
                    with tasks_lock:
                        task['output_file'] = m_ea.group(1).strip()
                        task['status'] = 'merging'
                        task['percent'] = 100.0

                # 检测最终文件名
                m = re.search(r'\[(?:Merger|download)\].*?"([^"]+)"', line)
                if m:
                    with tasks_lock:
                        task['output_file'] = m.group(1)

                # 检测已完成
                if 'has already been downloaded' in line:
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        with tasks_lock:
                            task['status'] = 'completed'
                            task['percent'] = 100.0
                            task['output_file'] = m.group(1)

                # 检测 [download] Destination 行获取文件名
                m2 = re.search(r'\[download\]\s+Destination:\s+(.+)', line)
                if m2:
                    with tasks_lock:
                        task['output_file'] = m2.group(1).strip()

                now = time.time()
                if now - last_update > 0.5:
                    last_update = now

            process.wait()
            ret = process.returncode

            if ret == 0:
                resolved = None
                with tasks_lock:
                    task['status'] = 'completed'
                    task['percent'] = 100.0
                    task['completed_at'] = time.time()

                    # 尝试找到输出文件
                    # 如果 output_file 指向的文件不存在 (如 .webm 被音频提取后删除), 重新扫描
                    output_path = DOWNLOAD_DIR / task['output_file'] if task.get('output_file') else None
                    if not task.get('output_file') or (output_path and not output_path.exists()):
                        files = sorted(DOWNLOAD_DIR.glob('*'), key=lambda f: f.stat().st_mtime, reverse=True)
                        for f in files:
                            if f.is_file() and f.suffix in ['.mp4', '.mkv', '.webm', '.mp3', '.m4a']:
                                task['output_file'] = f.name
                                break
                    resolved = task.get('output_file')
                if resolved:
                    new_name = ensure_browser_playable(resolved, task)
                    with tasks_lock:
                        task['output_file'] = new_name
                        task['status'] = 'completed'
                        task['percent'] = 100.0
                download_success = True
                break  # 下载成功, 退出重试循环
            else:
                # TikTok 失败时自动重试; 非 TikTok 走下面的 fallback
                if is_tiktok and attempt < max_attempts:
                    continue
                last_error = last_error or f'yt-dlp 退出码 {ret}'
                break

        except FileNotFoundError:
            with tasks_lock:
                task['status'] = 'failed'
                task['error'] = 'yt-dlp 未安装'
            return
        except Exception as e:
            last_error = str(e)
            if is_tiktok and attempt < max_attempts:
                continue
            break

    # 所有重试都失败后, 尝试智能提取真实视频地址 (非 TikTok 的 fallback)
    if not download_success and not pre_extracted_url:
        real_url = extract_real_video_url(url)
        if real_url and real_url != '__JS_CHALLENGE__' and real_url != url:
            with tasks_lock:
                task['status'] = 'downloading'
                task['percent'] = 0
                task['error'] = ''
                task['output_file'] = ''
            # 用提取到的真实地址重新构建命令并运行
            retry_cmd = [c for c in cmd if c != url]
            # 加上原始页面的Referer (CDN通常需要验证来源)
            from urllib.parse import urlparse
            origin = urlparse(url)
            referer = f"{origin.scheme}://{origin.netloc}/"
            retry_cmd.extend(['--add-header', f'Referer: {referer}'])
            retry_cmd.append(real_url)
            try:
                process2 = subprocess.Popen(
                    retry_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(DOWNLOAD_DIR),
                )
                with tasks_lock:
                    task['pid'] = process2.pid
                    task['process'] = process2
                for line2 in process2.stdout:
                    line2 = line2.strip()
                    if not line2:
                        continue
                    if line2.startswith('P|'):
                        parts2 = line2.split('|')
                        if len(parts2) >= 7:
                            with tasks_lock:
                                pct_s = parts2[1].strip().replace('%', '')
                                try:
                                    task['percent'] = float(pct_s)
                                except ValueError:
                                    pass
                                task['total_size'] = parts2[2].strip() if parts2[2] != 'NA' else ''
                                task['speed'] = parts2[3].strip() if parts2[3] != 'NA' else ''
                                task['eta'] = parts2[4].strip() if parts2[4] != 'NA' else ''
                    if '[Merger]' in line2 or '[ffmpeg]' in line2:
                        with tasks_lock:
                            task['status'] = 'merging'
                            task['percent'] = 100.0
                    m_ea2 = re.search(r'\[ExtractAudio\]\s+Destination:\s+(.+)', line2)
                    if m_ea2:
                        with tasks_lock:
                            task['output_file'] = m_ea2.group(1).strip()
                    m_fn = re.search(r'\[(?:Merger|download)\].*?"([^"]+)"', line2)
                    if m_fn:
                        with tasks_lock:
                            task['output_file'] = m_fn.group(1)
                    m_dest = re.search(r'\[download\]\s+Destination:\s+(.+)', line2)
                    if m_dest:
                        with tasks_lock:
                            task['output_file'] = m_dest.group(1).strip()
                process2.wait()
                ret2 = process2.returncode
                if ret2 == 0:
                    resolved2 = None
                    with tasks_lock:
                        task['status'] = 'completed'
                        task['percent'] = 100.0
                        task['completed_at'] = time.time()
                        if not task.get('output_file'):
                            files = sorted(DOWNLOAD_DIR.glob('*'), key=lambda f: f.stat().st_mtime, reverse=True)
                            for f in files:
                                if f.is_file() and f.suffix in ['.mp4', '.mkv', '.webm', '.mp3', '.m4a']:
                                    task['output_file'] = f.name
                                    break
                        resolved2 = task.get('output_file')
                    if resolved2:
                        new_name2 = ensure_browser_playable(resolved2, task)
                        with tasks_lock:
                            task['output_file'] = new_name2
                            task['status'] = 'completed'
                            task['percent'] = 100.0
                else:
                    with tasks_lock:
                        task['status'] = 'failed'
                        task['error'] = f'智能提取后下载仍失败 (退出码 {ret2})'
            except Exception as e2:
                with tasks_lock:
                    task['status'] = 'failed'
                    task['error'] = f'重试失败: {str(e2)}'
        elif real_url == '__JS_CHALLENGE__':
            with tasks_lock:
                task['status'] = 'failed'
                task['error'] = '该网站需要 JS 安全验证, yt-dlp 无法直接下载 (如论坛等)'
        else:
            with tasks_lock:
                task['status'] = 'failed'
                task['error'] = last_error or '下载失败'


@app.route('/')
def index():
    return render_template('index.html')


# === 认证接口 ===
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    ip = get_client_ip()

    if check_login_lock(ip):
        return jsonify({'error': '尝试次数过多，请 5 分钟后再试'}), 429

    if not username or not password:
        return jsonify({'error': '请输入用户名和密码'}), 400

    if verify_user(username, password):
        clear_login_attempts(ip)
        session.permanent = True
        session['username'] = username
        return jsonify({'status': 'ok', 'username': username})
    else:
        record_failed_login(ip)
        remaining = MAX_LOGIN_ATTEMPTS - login_attempts.get(ip, {}).get('count', 0)
        if remaining > 0:
            return jsonify({'error': f'用户名或密码错误，剩余 {remaining} 次尝试'}), 401
        else:
            return jsonify({'error': '尝试次数过多，请 5 分钟后再试'}), 429


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'status': 'ok'})


@app.route('/api/auth/check')
def auth_check():
    if 'username' in session:
        return jsonify({'logged_in': True, 'username': session['username']})
    return jsonify({'logged_in': False}), 401


@app.route('/api/change-password', methods=['POST'])
@login_required
def change_pw():
    data = request.json or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    if not old_password or not new_password:
        return jsonify({'error': '请填写完整'}), 400
    if len(new_password) < 6:
        return jsonify({'error': '新密码至少 6 位'}), 400

    if change_password(session['username'], old_password, new_password):
        return jsonify({'status': 'ok'})
    return jsonify({'error': '原密码错误'}), 400


@app.route('/api/change-username', methods=['POST'])
@login_required
def change_un():
    data = request.json or {}
    new_username = data.get('new_username', '').strip()
    password = data.get('password', '')

    if not new_username or not password:
        return jsonify({'error': '请填写完整'}), 400
    if len(new_username) < 3:
        return jsonify({'error': '用户名至少 3 个字符'}), 400

    if change_username(session['username'], new_username, password):
        session['username'] = new_username
        return jsonify({'status': 'ok', 'username': new_username})
    return jsonify({'error': '密码错误或用户名已存在'}), 400


@app.route('/api/proxy-config', methods=['GET'])
@login_required
def proxy_config_get():
    """获取当前代理配置"""
    return jsonify(douyin_downloader.get_proxy_config())


@app.route('/api/proxy-config', methods=['POST'])
@login_required
def proxy_config_post():
    """保存代理配置, 立即对后续下载生效 (无需重启)"""
    data = request.get_json(silent=True) or {}
    url = str(data.get('url') or '').strip()
    enabled = bool(data.get('enabled'))

    if enabled and not re.match(r'^(https?|socks4|socks4a|socks5|socks5h)://', url, re.I):
        return jsonify({'error': '代理地址需以 http://、https://、socks4:// 或 socks5:// 开头'}), 400

    cfg = douyin_downloader.save_proxy_config(enabled, url)
    logging.getLogger(__name__).info(
        f"代理配置已更新: {'启用 ' + cfg['url'] if cfg['enabled'] else '关闭'}")
    return jsonify({'status': 'ok', **cfg})


@app.route('/api/proxy-config/test', methods=['POST'])
@login_required
def proxy_config_test():
    """测试代理连通性 (不保存配置). 通过代理请求 google/youtube 204, 成功则附出口 IP"""
    data = request.get_json(silent=True) or {}
    test_url = str(data.get('url') or '').strip()
    if not test_url:
        test_url = douyin_downloader.get_proxy_config().get('url', '')
    if not test_url:
        return jsonify({'error': '请填写代理地址'}), 400
    if not re.match(r'^(https?|socks4|socks4a|socks5|socks5h)://', test_url, re.I):
        return jsonify({'error': '代理地址格式无效'}), 400

    proxies = {'http': test_url, 'https': test_url}
    import time as _time
    first_err = ''
    ok = False
    latency_ms = 0.0
    timed_out = False
    from curl_cffi import requests as cffi
    for target in ('https://www.googleapis.com/generate_204',
                   'https://www.youtube.com/generate_204'):
        try:
            t0 = _time.time()
            r = cffi.get(target, impersonate='chrome', timeout=10, proxies=proxies)
            latency_ms = (_time.time() - t0) * 1000
            if r.status_code in (200, 204):
                ok = True
                break
        except Exception as e:
            emsg = str(e)
            if 'timed out' in emsg or 'Timeout' in emsg:
                timed_out = True
            first_err = emsg

    if not ok:
        error = '连接超时' if timed_out else (first_err or '目标不可达')
        return jsonify({'ok': False, 'error': error}), 400

    # 获取代理出口 IP (失败不影响结果)
    ip = ''
    try:
        r = cffi.get('https://api.ipify.org/?format=json', impersonate='chrome', timeout=8,
                     proxies=proxies)
        if r.status_code == 200:
            ip = json.loads(r.text).get('ip', '')
    except Exception:
        pass
    logging.getLogger(__name__).info(
        f"代理连通测试通过: {test_url} {int(latency_ms)}ms" + (f" 出口IP {ip}" if ip else ""))
    return jsonify({'ok': True, 'latency_ms': round(latency_ms, 1), 'ip': ip})


@app.route('/api/douyin/info', methods=['POST'])
@login_required
def douyin_info():
    """预览抖音视频信息"""
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请输入URL'}), 400

    # 解析短链接
    resolved = douyin_downloader.resolve_short_url(url)
    aweme_id = douyin_downloader.extract_aweme_id(resolved)
    if not aweme_id:
        return jsonify({'error': '无法识别抖音视频链接'}), 400

    info = douyin_downloader.get_video_info(aweme_id)
    if not info['ok']:
        return jsonify({'error': info.get('error', '获取失败')}), 400

    return jsonify({
        'ok': True,
        'title': info['title'],
        'author': info['author'],
        'duration': round(info['duration'], 1),
        'width': info['width'],
        'height': info['height'],
        'size': info['size'],
        'size_str': f"{info['size']/1024/1024:.1f}MB" if info['size'] else '-',
        'cover': info['cover'],
    })


@app.route('/api/download', methods=['POST'])
@login_required
def start_download():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': '请输入 URL'}), 400

    if not re.match(r'https?://', url):
        return jsonify({'error': 'URL 格式错误'}), 400

    task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        tasks[task_id] = {
            'id': task_id,
            'url': url,
            'status': 'queued',
            'percent': 0,
            'speed': '',
            'eta': '',
            'total_size': '',
            'frag_current': 0,
            'frag_total': 0,
            'output_file': '',
            'error': '',
            'file_size': '',
            'started_at': None,
            'completed_at': None,
            'format': data.get('format', 'best'),
            'concurrent': data.get('concurrent', 10),
        }

    thread = threading.Thread(target=run_download, args=(task_id, url, data), daemon=True)
    thread.start()

    return jsonify({'task_id': task_id, 'status': 'queued'})


@app.route('/api/status/<task_id>')
@login_required
def get_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # 获取文件大小
    result = dict(task)
    if result.get('output_file'):
        filepath = DOWNLOAD_DIR / result['output_file']
        if filepath.exists():
            result['file_size'] = get_file_size_str(filepath.stat().st_size)
            result['file_size_bytes'] = filepath.stat().st_size
        # basename
        result['output_file'] = os.path.basename(result['output_file'])

    # 计算耗时
    if result.get('started_at'):
        end = result.get('completed_at') or time.time()
        elapsed = int(end - result['started_at'])
        m, s = divmod(elapsed, 60)
        result['elapsed'] = f"{m:02d}:{s:02d}"

    # 清理不可序列化的字段
    result.pop('process', None)
    result.pop('pid', None)

    return jsonify(result)


@app.route('/api/tasks')
@login_required
def list_tasks():
    with tasks_lock:
        all_tasks = []
        for t in tasks.values():
            result = dict(t)
            result.pop('process', None)
            result.pop('pid', None)
            if result.get('output_file'):
                result['output_file'] = os.path.basename(result['output_file'])
                filepath = DOWNLOAD_DIR / result['output_file']
                if filepath.exists():
                    result['file_size'] = get_file_size_str(filepath.stat().st_size)
            all_tasks.append(result)
    # 按时间倒序
    all_tasks.sort(key=lambda x: x.get('started_at') or 0, reverse=True)
    return jsonify(all_tasks)


@app.route('/api/cancel/<task_id>', methods=['POST'])
@login_required
def cancel_download(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    if 'process' in task and task['process']:
        task['process'].terminate()
        task['status'] = 'cancelled'
        return jsonify({'status': 'cancelled'})
    return jsonify({'error': '无法取消任务'}), 400


@app.route('/api/pause/<task_id>', methods=['POST'])
@login_required
def pause_download(task_id):
    """暂停下载: 发送 SIGSTOP 暂停 yt-dlp 进程"""
    import signal
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if 'process' in task and task['process']:
        try:
            task['process'].send_signal(signal.SIGSTOP)
            task['status'] = 'paused'
            return jsonify({'status': 'paused'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': '无法暂停任务'}), 400


@app.route('/api/resume/<task_id>', methods=['POST'])
@login_required
def resume_download(task_id):
    """继续下载: 发送 SIGCONT 恢复 yt-dlp 进程"""
    import signal
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if 'process' in task and task['process']:
        try:
            task['process'].send_signal(signal.SIGCONT)
            task['status'] = 'downloading'
            return jsonify({'status': 'downloading'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': '无法继续任务'}), 400


@app.route('/api/retry/<task_id>', methods=['POST'])
@login_required
def retry_download(task_id):
    """重试: 用原 URL 和选项重新下载"""
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # 如果还在运行, 先取消
    if 'process' in task and task['process']:
        try:
            task['process'].terminate()
        except Exception:
            pass

    # 用原参数重新启动
    url = task['url']
    options = {'format': task.get('format', 'best'), 'concurrent': task.get('concurrent', 10)}

    new_task_id = str(uuid.uuid4())[:8]
    with tasks_lock:
        # 删除旧任务
        if task_id in tasks:
            del tasks[task_id]
        tasks[new_task_id] = {
            'id': new_task_id,
            'url': url,
            'status': 'queued',
            'percent': 0,
            'speed': '',
            'eta': '',
            'total_size': '',
            'frag_current': 0,
            'frag_total': 0,
            'output_file': '',
            'error': '',
            'file_size': '',
            'started_at': None,
            'completed_at': None,
            'format': options['format'],
            'concurrent': options['concurrent'],
        }

    thread = threading.Thread(target=run_download, args=(new_task_id, url, options), daemon=True)
    thread.start()
    return jsonify({'task_id': new_task_id, 'status': 'queued'})


@app.route('/api/delete/<task_id>', methods=['POST'])
@login_required
def delete_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({'error': '任务不存在'}), 404

        # 如果有文件，删除文件
        if task.get('output_file'):
            filepath = DOWNLOAD_DIR / task['output_file']
            if filepath.exists():
                filepath.unlink()

        del tasks[task_id]
    return jsonify({'status': 'deleted'})


@app.route('/api/files')
@login_required
def list_files():
    files = []
    for f in sorted(DOWNLOAD_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and not f.name.startswith('.'):
            stat = f.stat()
            files.append({
                'name': f.name,
                'size': get_file_size_str(stat.st_size),
                'size_bytes': stat.st_size,
                'modified': stat.st_mtime,
            })
    return jsonify(files)


@app.route('/api/file/stream')
@login_required
def stream_file_query():
    """流式播放(query 参数版): 规避文件名含特殊字符时的路径编码歧义"""
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '缺少 name 参数'}), 400
    filepath = DOWNLOAD_DIR / filename
    base = DOWNLOAD_DIR.resolve()
    if base not in filepath.resolve().parents:
        return jsonify({'error': '非法的文件名'}), 400
    # 边下边播: 如果正式文件不存在, 尝试 .part 文件
    if not filepath.exists():
        part_path = DOWNLOAD_DIR / (filename + '.part')
        if part_path.exists():
            filepath = part_path
        else:
            return jsonify({'error': '文件不存在'}), 404
    mime_type, _ = mimetypes.guess_type(str(filepath))
    return send_file(str(filepath), mimetype=mime_type, conditional=True)


@app.route('/api/file', methods=['GET'])
@login_required
def download_file_query():
    """下载文件(query 参数版)"""
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '缺少 name 参数'}), 400
    filepath = DOWNLOAD_DIR / filename
    base = DOWNLOAD_DIR.resolve()
    if base not in filepath.resolve().parents:
        return jsonify({'error': '非法的文件名'}), 400
    if not filepath.exists():
        return jsonify({'error': '文件不存在'}), 404
    return send_file(str(filepath), as_attachment=True, download_name=filename)


@app.route('/api/file', methods=['DELETE'])
@login_required
def delete_file_query():
    """删除文件(query 参数版)"""
    filename = request.args.get('name', '')
    if not filename:
        return jsonify({'error': '缺少 name 参数'}), 400
    filepath = DOWNLOAD_DIR / filename
    base = DOWNLOAD_DIR.resolve()
    if base not in filepath.resolve().parents:
        return jsonify({'error': '非法的文件名'}), 400
    if not filepath.exists():
        return jsonify({'error': '文件不存在'}), 404
    filepath.unlink()
    return jsonify({'status': 'deleted'})


@app.route('/api/file/<path:filename>')
@login_required
def download_file(filename):
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        return jsonify({'error': '文件不存在'}), 404

    mime_type, _ = mimetypes.guess_type(str(filepath))
    return send_file(str(filepath), as_attachment=True, download_name=filename)


@app.route('/api/file/<path:filename>/stream')
@login_required
def stream_file(filename):
    filepath = DOWNLOAD_DIR / filename
    # 边下边播: 如果正式文件不存在, 尝试 .part 文件
    if not filepath.exists():
        part_path = DOWNLOAD_DIR / (filename + '.part')
        if part_path.exists():
            filepath = part_path
        else:
            return jsonify({'error': '文件不存在'}), 404

    mime_type, _ = mimetypes.guess_type(str(filepath))
    # conditional=True 支持 HTTP Range 请求, 允许边下边播和拖动进度条
    return send_file(str(filepath), mimetype=mime_type, conditional=True)


@app.route('/api/file/<path:filename>', methods=['DELETE'])
@login_required
def delete_file(filename):
    filepath = DOWNLOAD_DIR / filename
    if not filepath.exists():
        return jsonify({'error': '文件不存在'}), 404
    filepath.unlink()
    return jsonify({'status': 'deleted'})


@app.route('/api/clear-cache', methods=['POST'])
@login_required
def clear_cache():
    """清除下载缓存: 删除所有临时文件(.part/.ytdl/--Frag*), 清除已取消/失败的任务记录"""
    deleted_files = []
    freed_bytes = 0

    # 删除临时文件 (.part, .part-Frag*, .ytdl, --Frag*, .m3u8临时文件)
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and (
            f.name.endswith('.part') or
            f.name.endswith('.ytdl') or
            f.name.endswith('.m3u8') or
            '.part-Frag' in f.name or
            '--Frag' in f.name or
            f.name.startswith('--Frag') or
            re.search(r'-Frag\d+$', f.name) or
            f.name.startswith('tmp') and f.suffix in ('.m3u8', '.mp4', '.ts')
        ):
            size = f.stat().st_size
            freed_bytes += size
            deleted_files.append(f.name)
            f.unlink()

    # 清除已取消/失败的任务记录 (保留 downloading/completed/queued/merging)
    removed_tasks = []
    with tasks_lock:
        to_remove = []
        for tid, t in tasks.items():
            if t.get('status') in ('cancelled', 'failed'):
                to_remove.append(tid)
                removed_tasks.append(tid)
        for tid in to_remove:
            del tasks[tid]

    return jsonify({
        'status': 'ok',
        'deleted_files': deleted_files,
        'freed_space': get_file_size_str(freed_bytes),
        'removed_tasks': removed_tasks,
        'files_count': len(deleted_files),
        'tasks_count': len(removed_tasks),
    })


@app.route('/api/clear-tasks', methods=['POST'])
@login_required
def clear_tasks():
    """清除所有已完成/取消/失败的任务记录 (不删文件)"""
    removed = []
    with tasks_lock:
        to_remove = []
        for tid, t in tasks.items():
            if t.get('status') in ('completed', 'cancelled', 'failed'):
                to_remove.append(tid)
                removed.append(tid)
        for tid in to_remove:
            del tasks[tid]
    return jsonify({'status': 'ok', 'removed_tasks': removed, 'count': len(removed)})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5200))
    print(f"🚀 fan-video-dl 启动于 http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
