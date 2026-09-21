修复：yt-dlp 自动定位新增 python -m yt_dlp 模块回退，启动时输出定位日志便于排查

```bash
docker pull mobufan/fan-video-dl:latest
```
```bash
docker pull mobufan/fan-video-dl:v1.0.2
```

```bash
docker pull ghcr.io/meimolihan/fan-video-dl:latest
```
```bash
docker pull ghcr.io/meimolihan/fan-video-dl:v1.0.2
```

## 一键脚本安装（systemd）
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/install.sh)" -p 5200 -d /var/lib/fan-video-dl -y
```

## 一键脚本卸载
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/uninstall.sh)" -y --purge
```
