日常更新

- 修复 GitHub Actions 警告：dockerhub-description 升级至 v5 (Node 24)，Dockerfile 移除敏感 ENV，runner 固定 ubuntu-24.04
- docker-compose 支持通过 .env 覆盖账号密码

```bash
docker pull mobufan/fan-video-dl:latest
```
```bash
docker pull mobufan/fan-video-dl:v1.2.5
```

```bash
docker pull ghcr.io/meimolihan/fan-video-dl:latest
```
```bash
docker pull ghcr.io/meimolihan/fan-video-dl:v1.2.5
```

## 一键脚本安装（systemd）
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/install.sh)" -p 5200 -d /var/lib/fan-video-dl -y
```

## 一键脚本卸载
```bash
bash -c "$(curl -sSL https://raw.githubusercontent.com/meimolihan/fan-video-dl/main/scripts/uninstall.sh)" -y --purge
```
