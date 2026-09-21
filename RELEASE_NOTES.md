修复: AV1/VP9 视频输出转码为 H.264, 解决浏览器无法播放

```bash
docker pull mobufan/fan-video-dl:latest
```
```bash
docker pull mobufan/fan-video-dl:v1.1.5
```

```bash
docker pull ghcr.io/meimolihan/fan-video-dl:latest
```
```bash
docker pull ghcr.io/meimolihan/fan-video-dl:v1.1.5
```

## 一键脚本安装（systemd）
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/install.sh)" -p 5200 -d /var/lib/fan-video-dl -y
```

## 一键脚本卸载
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/uninstall.sh)" -y --purge
```
