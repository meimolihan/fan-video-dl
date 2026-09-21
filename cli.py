#!/usr/bin/env python3
"""
fan-video-dl 内置 CLI 管理命令

  fan-video-dl status
  fan-video-dl credentials
  fan-video-dl uninstall [-y|--yes] [--purge|--keep-data]
  fan-video-dl start | stop | restart
  fan-video-dl version | -version | --version | -v
  fan-video-dl help | -h | --help

纯 Python 标准库实现，无第三方依赖。安装约定与 scripts/install.sh、scripts/uninstall.sh 保持一致。
开源地址: https://github.com/meimolihan/fan-video-dl
"""

import os
import sys
import glob
import shutil
import socket
import subprocess
from pathlib import Path

APP_NAME = "fan-video-dl"
SERVICE_NAME = "fan-video-dl"
SERVICE_FILE = f"/etc/systemd/system/{SERVICE_NAME}.service"
RECORD_FILE = f"/etc/{APP_NAME}.conf"
BIN_NAME = APP_NAME
WRAPPER_FILE = f"/usr/local/bin/{BIN_NAME}"
# 默认程序目录：取 CLI 自身所在目录（systemd 安装为 /var/lib/fan-video-dl）
DEFAULT_APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = f"/var/lib/{APP_NAME}/data"
DEFAULT_PORT = 5200

VERSION = "unknown"
try:
    vf = Path(__file__).resolve().parent / "version.txt"
    if vf.exists():
        VERSION = vf.read_text(encoding="utf-8").strip() or "unknown"
except Exception:
    VERSION = "unknown"

# ================== 终端配色（与 install.sh / uninstall.sh 对齐） ==================
C = {
    "grey": "\033[38;5;59m",
    "red": "\033[38;5;9m",
    "green": "\033[38;5;10m",
    "yellow": "\033[38;5;11m",
    "blue": "\033[38;5;32m",
    "white": "\033[38;5;15m",
    "purple": "\033[38;5;13m",
    "cyan": "\033[38;5;14m",
    "reset": "\033[0m",
}


def paint(s, color):
    return f"{color}{s}{C['reset']}"


def err(*a):
    print(f"  {paint('[错误]', C['red'])} {' '.join(str(x) for x in a)}")


def warn(*a):
    print(f"  {paint('[警告]', C['yellow'])} {' '.join(str(x) for x in a)}")


def done(*a):
    print(f"  {paint('✔', C['green'])} {' '.join(str(x) for x in a)}")


def section(title):
    print(f"  {paint('▶', C['purple'])} {title}")


def sep():
    print(paint("————————————————————————————————————————————————", C["cyan"]))


def kv(k, v):
    print(f"  {paint(str(k).ljust(16), C['blue'])} {paint(str(v), C['white'])}")


def banner(title):
    print(paint(f"""
   ███████╗ █████╗ ███╗   ██╗    ██╗   ██╗██╗██████╗ ███████╗ ██████╗     ██████╗ ██╗
   ██╔════╝██╔══██╗████╗  ██║    ██║   ██║██║██╔══██╗██╔════╝██╔═══██╗    ██╔══██╗██║
   █████╗  ███████║██╔██╗ ██║    ██║   ██║██║██║  ██║█████╗  ██║   ██║    ██║  ██║██║
   ██╔══╝  ██╔══██║██║╚██╗██║    ╚██╗ ██╔╝██║██║  ██║██╔══╝  ██║   ██║    ██║  ██║██║
   ██║     ██║  ██║██║ ╚████║     ╚████╔╝ ██║██████╔╝███████╗╚██████╔╝    ██████╔╝███████╗
   ╚═╝     ╚═╝  ╚═╝╚═╝  ╚═══╝      ╚═══╝  ╚═╝╚═════╝ ╚══════╝ ╚═════╝     ╚═════╝ ╚═════╝""", C["purple"]))
    print(f"{paint(f'{BIN_NAME} v{VERSION}', C['white'])} — {paint(title, C['cyan'])}\n")


# ================== 通用工具 ==================
def read_record(key):
    try:
        for line in Path(RECORD_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip() or None
    except Exception:
        pass
    return None


def resolve_paths():
    app_dir = read_record("APP_DIR") or str(DEFAULT_APP_DIR)
    return {
        "app_dir": app_dir,
        "data_dir": read_record("DATA_DIR") or DEFAULT_DATA_DIR,
        "backup_dir": read_record("BACKUP_DIR") or os.path.join(app_dir, "backup"),
        "port": read_record("PORT") or str(DEFAULT_PORT),
    }


def has_cmd(name):
    return shutil.which(name) is not None


def run(name, args, quiet=True):
    """执行命令并返回是否成功 (以退出码为准)"""
    if not has_cmd(name):
        return False
    try:
        proc = subprocess.run([name, *args],
                              stdout=subprocess.DEVNULL if quiet else None,
                              stderr=subprocess.DEVNULL if quiet else None,
                              timeout=60)
        return proc.returncode == 0
    except Exception:
        return False


def read_proc_field(pid, field):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(f"{field}:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""


def proc_uptime_sec(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        close_idx = stat.rfind(")")
        fields = stat[close_idx + 2:].split()
        start_ticks = float(fields[19])
        clk = os.sysconf("SC_CLK_TCK") or 100
        up_sec = float(Path("/proc/uptime").read_text().split()[0])
        elapsed = int(up_sec) - start_ticks / clk
        return int(elapsed) if elapsed > 0 else 0
    except Exception:
        return 0


def format_uptime(sec):
    if not sec or sec <= 0:
        return "未知"
    return f"{sec // 3600}小时 {(sec % 3600) // 60}分钟 {sec % 60}秒"


def format_mem(raw):
    if not raw:
        return "未知"
    parts = raw.split()
    try:
        kb = float(parts[0])
    except (ValueError, IndexError):
        return raw
    mb = kb / 1024
    if mb >= 1024:
        return f"{raw}（{mb / 1024:.2f} GB）"
    return f"{raw}（{mb:.2f} MB）"


def human_size(num):
    num = float(num)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} EB"


def is_virtual_iface(name):
    return (name == "docker0" or name == "docker_gwbridge"
            or name.startswith(("br-", "veth", "virbr", "vnet", "vmnet")))


def local_ipv4_addrs():
    addrs = []
    try:
        import socket as _s
        for name, _ in _s.if_nameindex():
            if is_virtual_iface(name):
                continue
            try:
                out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", name],
                                     capture_output=True, text=True).stdout
                for line in out.splitlines():
                    parts = line.split()
                    if "inet" in parts:
                        ip = parts[parts.index("inet") + 1].split("/")[0]
                        if ip and not ip.startswith("127.") and ip not in addrs:
                            addrs.append(ip)
            except Exception:
                continue
    except Exception:
        pass
    return addrs


def print_access(port):
    p = port or DEFAULT_PORT
    kv("Local access", f"http://localhost:{p}")
    ips = local_ipv4_addrs()
    if not ips:
        kv("Network access", "未检测到局域网 IPv4 地址")
        return
    for ip in ips:
        kv("Network access", f"http://{ip}:{p}")


def cmdline_of(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
    except Exception:
        return ""


def match_app_procs(app_dir):
    marker = os.path.join(app_dir, "venv", "bin", "gunicorn")
    marker2 = os.path.join(app_dir, "app.py")
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid <= 0 or pid == os.getpid():
            continue
        cmd = cmdline_of(pid)
        if marker in cmd or marker2 in cmd:
            pids.append(pid)
    return sorted(pids)


def socket_inodes(pid):
    inodes = set()
    try:
        for name in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{name}")
                if target.startswith("socket:["):
                    inodes.add(target[8:-1])
            except Exception:
                continue
    except Exception:
        pass
    return inodes


def listen_ports(pid):
    inodes = socket_inodes(pid)
    ports = []
    for proto in ("tcp", "tcp6"):
        try:
            data = Path(f"/proc/{pid}/net/{proto}").read_text().splitlines()[1:]
        except Exception:
            continue
        for line in data:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            if fields[9] not in inodes:
                continue
            port = int(fields[1].split(":")[1], 16)
            if port > 0 and port not in ports:
                ports.append(port)
    return ports


def listen_port(pid, prefer):
    ports = listen_ports(pid)
    try:
        want = int(prefer)
    except (TypeError, ValueError):
        want = None
    if want in ports:
        return str(want)
    return str(ports[0]) if ports else ""


def find_process(app_dir, prefer_port):
    # 1) systemd
    if has_cmd("systemctl"):
        try:
            out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", SERVICE_NAME],
                                 capture_output=True, text=True).stdout.strip()
            if out.isdigit() and int(out) > 0 and os.path.exists(f"/proc/{out}"):
                return f"systemd（{SERVICE_NAME}.service）", int(out)
        except Exception:
            pass
    # 2) docker
    if has_cmd("docker"):
        try:
            names = subprocess.run(["docker", "ps", "--filter", f"name={BIN_NAME}", "--format", "{{.Names}}"],
                                   capture_output=True, text=True).stdout.split()
            for n in names:
                out = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", n],
                                     capture_output=True, text=True).stdout.strip()
                if out.isdigit() and int(out) > 0:
                    return f"docker（容器 {n}）", int(out)
        except Exception:
            pass
    # 3) /proc 扫描
    pids = match_app_procs(app_dir)
    for p in pids:
        if listen_port(p, prefer_port):
            return "直接运行（/proc）", p
    if pids:
        return "直接运行（/proc）", pids[0]
    return "", 0


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def confirm(question, def_yes):
    if not sys.stdin.isatty():
        return def_yes
    tip = "[Y/n]" if def_yes else "[y/N]"
    try:
        ans = input(f"  {question} {paint(tip, C['yellow'])}: ").strip().lower()
    except EOFError:
        return def_yes
    if ans == "":
        return def_yes
    return ans in ("y", "yes")


def close_firewall_port(port):
    p = str(port)
    if has_cmd("firewall-cmd"):
        try:
            state = subprocess.run(["firewall-cmd", "--state"], capture_output=True, text=True).stdout.strip()
            if state == "running":
                run("firewall-cmd", ["--permanent", f"--remove-port={p}/tcp"])
                run("firewall-cmd", ["--reload"])
                done(f"已通过 firewalld 关闭端口 {p}/tcp")
                return
        except Exception:
            pass
    if has_cmd("ufw"):
        try:
            state = subprocess.run(["ufw", "status"], capture_output=True, text=True).stdout
            if "active" in state:
                run("ufw", ["delete", "allow", f"{p}/tcp"])
                done(f"已通过 ufw 关闭端口 {p}/tcp")
                return
        except Exception:
            pass
    if has_cmd("iptables"):
        if run("iptables", ["-D", "INPUT", "-p", "tcp", "--dport", p, "-j", "ACCEPT"]):
            done(f"已通过 iptables 关闭端口 {p}/tcp")


# ================== status ==================
def cmd_status():
    paths = resolve_paths()
    banner("服务状态")
    sep()
    record_port = read_record("PORT")
    if record_port:
        kv("install 记录端口", record_port)
    record_data = read_record("DATA_DIR")
    if record_data:
        kv("install 记录数据目录", record_data)

    ptype, pid = find_process(paths["app_dir"], paths["port"])
    if not ptype or not pid:
        err(f"{APP_NAME} 服务未运行")
        sep()
        return 1

    section("服务方式")
    kv("运行方式", ptype)
    if ptype.startswith("systemd"):
        active = run("systemctl", ["is-active", "--quiet", SERVICE_NAME])
        kv("服务状态", "运行中" if active else "服务异常（is-active 非 active）")
    else:
        kv("服务状态", f"运行中（{ptype}）")

    section("进程信息")
    kv("进程 PID", pid)
    kv("线程数量", read_proc_field(pid, "Threads") or "未知")

    section("网络")
    lp = listen_port(pid, paths["port"])
    kv("监听端口", lp or "未找到")
    print_access(lp or paths["port"])

    section("运行时间")
    kv("已运行", format_uptime(proc_uptime_sec(pid)))

    section("内存")
    kv("虚拟内存", format_mem(read_proc_field(pid, "VmSize")))
    kv("物理内存", format_mem(read_proc_field(pid, "VmRSS")))
    try:
        kv("打开文件", str(len(os.listdir(f"/proc/{pid}/fd"))))
    except Exception:
        pass

    section("路径")
    kv("程序目录", paths["app_dir"])
    kv("数据目录", paths["data_dir"])
    kv("备份目录", paths["backup_dir"])
    kv("安装记录", RECORD_FILE)
    kv("Python 版本", sys.version.split()[0])

    sep()
    return 0


# ================== credentials ==================
def cmd_credentials():
    paths = resolve_paths()
    banner("登录凭据")
    sep()
    user = read_record("AUTH_USERNAME") or "admin"
    pwd = read_record("AUTH_PASSWORD")
    kv("用户名", user)
    if pwd:
        kv("密码", pwd)
        print(paint("  初始/配置密码，登录后请及时在「账户」中修改（修改后配置文件不再同步）", C["grey"]))
    else:
        kv("密码", "已修改（初始密码不可再查询，请使用自定义密码登录）")
    section("访问地址")
    print_access(paths["port"])
    sep()
    return 0


# ================== uninstall ==================
def uninstall_usage():
    for line in [
        f"用法: {BIN_NAME} uninstall [选项]",
        "",
        "选项:",
        "    -y, --yes        免确认，静默卸载（默认保留数据目录）",
        "    --purge          卸载时同时删除数据目录",
        "    --keep-data      卸载时保留数据目录",
        "    -h, --help       显示帮助",
        "",
        "示例:",
        f"    sudo {BIN_NAME} uninstall -y           免确认卸载，保留数据目录",
        f"    sudo {BIN_NAME} uninstall -y --purge   免确认卸载，并删除数据目录",
    ]:
        print(paint(line, C["white"]))


def cmd_uninstall(args):
    yes = purge = keep = False
    for a in args:
        if a in ("-y", "--yes"):
            yes = True
        elif a in ("--purge", "--delete-data"):
            purge = True
        elif a == "--keep-data":
            keep = True
        elif a in ("-h", "--help"):
            uninstall_usage()
            return 0
        else:
            err(f"未知参数: {a}，使用 -h 查看帮助")
            return 1
    if hasattr(os, "getuid") and os.getuid() != 0:
        err(f"请以 root 身份运行：sudo {BIN_NAME} uninstall")
        return 1
    if purge and keep:
        err("--purge 与 --keep-data 不能同时使用")
        return 1

    paths = resolve_paths()
    app_dir = paths["app_dir"]
    data_dir = paths["data_dir"]
    banner("卸载")
    sep()

    if not yes and not confirm(f"卸载将停止并移除 {APP_NAME} 服务与程序，是否继续", False):
        print(paint("已取消卸载。", C["yellow"]))
        return 0

    close_firewall_port(paths["port"] or DEFAULT_PORT)

    if os.path.exists(SERVICE_FILE):
        done(f"正在停止并移除 systemd 服务 {APP_NAME} ...")
        run("systemctl", ["stop", SERVICE_NAME])
        run("systemctl", ["disable", SERVICE_NAME])
        try:
            os.remove(SERVICE_FILE)
        except OSError:
            pass
        run("systemctl", ["daemon-reload"])
        run("systemctl", ["reset-failed"])

    if has_cmd("docker"):
        try:
            names = subprocess.run(["docker", "ps", "-a", "--filter", f"name={BIN_NAME}", "--format", "{{.Names}}"],
                                   capture_output=True, text=True).stdout.split()
            for n in names:
                if n == BIN_NAME:
                    done(f"正在移除容器 {n} ...")
                    run("docker", ["rm", "-f", n], quiet=False)
        except Exception:
            pass

    pids = match_app_procs(app_dir)
    if pids:
        done(f"正在停止 {APP_NAME} 进程: {' '.join(map(str, pids))} ...")
        import time
        for pid in pids:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        time.sleep(1)
        for pid in pids:
            if os.path.exists(f"/proc/{pid}"):
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

    if os.path.exists(app_dir):
        try:
            shutil.rmtree(app_dir)
            done(f"已删除程序目录 {app_dir}")
        except OSError as e:
            warn(f"删除程序目录 {app_dir} 失败: {e}")
    else:
        done(f"未找到程序目录 {app_dir}，跳过。")

    if os.path.exists(WRAPPER_FILE) or os.path.islink(WRAPPER_FILE):
        try:
            os.remove(WRAPPER_FILE)
            done(f"已删除命令 {WRAPPER_FILE}")
        except OSError:
            pass

    if os.path.isdir(data_dir):
        done(f"检测到数据目录: {data_dir}（约 {human_size(dir_size(data_dir))}）")
        if purge:
            remove = True
        elif keep:
            remove = False
        elif yes:
            remove = False
        else:
            remove = confirm(f"是否删除数据目录 {data_dir}（完全卸载）", True)
        if remove:
            try:
                shutil.rmtree(data_dir)
                done(f"已删除数据目录 {data_dir}")
            except OSError as e:
                warn(f"删除数据目录失败: {e}")
        else:
            done(f"已保留数据目录 {data_dir}")
    else:
        done(f"未检测到数据目录 {data_dir}，跳过删除。")

    try:
        os.remove(RECORD_FILE)
        done(f"已删除安装记录 {RECORD_FILE}")
    except OSError:
        pass

    done(f"{APP_NAME} 卸载完成")
    print(paint("如需重新安装，请重新运行 scripts/install.sh。", C["grey"]))
    return 0


# ================== start / stop / restart ==================
def cmd_service(action):
    if not has_cmd("systemctl"):
        err("当前环境没有 systemd，无法执行该操作，请手动管理进程。")
        return 1
    if not os.path.exists(SERVICE_FILE):
        err(f"未找到 systemd 服务 {SERVICE_FILE}，请先运行 scripts/install.sh 安装。")
        return 1
    label = {"start": "启动", "stop": "停止", "restart": "重启"}.get(action, action)
    banner(f"{label}服务")
    if not run("systemctl", [action, SERVICE_NAME], quiet=False):
        err(f"systemctl {action} {SERVICE_NAME} 执行失败")
        sep()
        return 1
    done(f"systemctl {action} {SERVICE_NAME} 完成")
    return 0


# ================== help / version ==================
def cmd_help():
    banner("管理命令")
    rows = [
        ["status", "显示运行方式（systemd / Docker / 直接运行）、PID、端口、访问地址、运行时长、内存、路径"],
        ["credentials", "显示登录用户名与初始密码（修改密码后不再显示）"],
        ["start | stop | restart", "启动 / 停止 / 重启 systemd 服务"],
        ["uninstall [-y] [--purge|--keep-data]", "停止并移除服务/容器/进程，删除程序与安装记录；可选删除数据目录"],
        ["version, -version, --version, -v", "显示版本号"],
        ["help, -h, --help", "显示本帮助"],
    ]
    for cmd, desc in rows:
        print(f"  {paint(cmd.ljust(40), C['white'])} {paint(desc, C['grey'])}")
    section("访问地址")
    print_access(resolve_paths()["port"])
    print("")
    print(f"  {paint('使用示例：', C['cyan'])}")
    print(f"    {paint(f'{BIN_NAME} status', C['green'])}")
    print(f"    {paint(f'{BIN_NAME} credentials', C['green'])}")
    print(f"    {paint(f'sudo {BIN_NAME} uninstall -y            # 免确认卸载，保留数据目录', C['green'])}")
    print(f"    {paint(f'sudo {BIN_NAME} uninstall -y --purge    # 免确认卸载，并删除数据目录', C['green'])}")
    return 0


def cmd_version():
    print(f"{BIN_NAME} {VERSION}")
    return 0


# ================== 入口 ==================
def main():
    args = sys.argv[1:]
    if not args:
        return cmd_help()
    sub, rest = args[0], args[1:]
    if sub == "status":
        return cmd_status()
    if sub in ("credentials", "credential", "password", "pwd"):
        return cmd_credentials()
    if sub == "uninstall":
        return cmd_uninstall(rest)
    if sub in ("start", "stop", "restart"):
        return cmd_service(sub)
    if sub in ("version", "-version", "--version", "-v"):
        return cmd_version()
    if sub in ("help", "-h", "--help"):
        return cmd_help()
    err(f"未知命令: {sub}")
    print("")
    cmd_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        err(str(e))
        sys.exit(1)
