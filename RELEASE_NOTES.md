修复: CLI 版本号重复 v

```bash
docker pull mobufan/fan-video-dl:latest
```
```bash
docker pull mobufan/fan-video-dl:v1.1.6
```

```bash
docker pull ghcr.io/meimolihan/fan-video-dl:latest
```
```bash
docker pull ghcr.io/meimolihan/fan-video-dl:v1.1.6
```

## 一键脚本安装（systemd）
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/install.sh)" -p 5200 -d /var/lib/fan-video-dl -y
```

## 一键脚本卸载
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/uninstall.sh)" -y --purge
```
