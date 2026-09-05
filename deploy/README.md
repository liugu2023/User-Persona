# 部署说明（systemd）

公网云服务器（Linux）上的标准部署方式。零第三方依赖，系统 `python3` 即可运行。

## 1. 上传代码

```bash
sudo mkdir -p /opt/feed-knows-you
sudo chown $USER /opt/feed-knows-you
git clone <你的仓库地址> /opt/feed-knows-you   # 或 scp 上传
```

## 2. 配置环境变量

```bash
sudo tee /etc/feed-knows-you.env >/dev/null <<'EOF'
YUNA_ADMIN_PASSWORD=换成你的现场管理员密钥
YUNA_METRICS_PATH=/opt/feed-knows-you/demo/aggregate_metrics.json
EOF
sudo chmod 600 /etc/feed-knows-you.env
sudo chown www-data /opt/feed-knows-you/demo   # WorkingDirectory 需可写（聚合统计与日志落在这里）
```

## 3. 安装并启动服务

```bash
sudo cp deploy/feed-knows-you.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feed-knows-you
```

## 4. 日常管理

| 操作 | 命令 |
|------|------|
| 启动 / 停止 | `sudo systemctl start\|stop feed-knows-you` |
| 重启 | `sudo systemctl restart feed-knows-you` |
| 状态 | `systemctl status feed-knows-you` |
| 实时日志 | `tail -f /opt/feed-knows-you/demo/server.log` |
| 错误日志 | `tail -f /opt/feed-knows-you/demo/server.err.log` |
| 开机自启 | `sudo systemctl enable feed-knows-you` |

日志说明：服务把 stdout/stderr 追加写入 `demo/server.log` 与 `demo/server.err.log`。
应用自身不打印访问日志（隐私设计，不记录 IP 与设备信息），日志里只有启动横幅和异常堆栈。
文件会缓慢增长，介意的话加一条 logrotate：

```
/opt/feed-knows-you/demo/server*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

## 5. 反向代理（生产建议）

服务本身是 HTTP；公网部署请在前置一层 HTTPS 反向代理（Nginx / Caddy），
二维码指向 HTTPS 域名。WebSocket/SSE 无需特殊配置，透传即可（大屏的
`/api/stream` 是普通的长连接响应流）。
