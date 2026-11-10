# Simular Box 保活脚本

一个文件、零依赖（Python 3.8+ 标准库）。让 Simular Box（box.simular.ai）不因空闲被
平台休眠：每 5 分钟通过 MCP 控制通道发一次活动信号（box_status），发现机器不在
运行态就自动 box_wake 唤醒。

**已部署**：一台常开 Linux 服务器，`simular-keepalive.timer` 每 300 秒一跑。

## 背景：为什么必须跑在 box 外面

实测 2026-11-10，两条独立证据证明「保活做在 box 自己上面」不可行：

1. **box 调不了自己的控制面**。box 无直连出网（DNS 全废、直连被拒），唯一出口是
   本机代理 `127.0.0.1:3128`（MITM）。该代理对多数域名放行，唯独挂起 CONNECT 到
   `mcp.box.simular.ai` —— 平台刻意禁止 box 自唤醒。
2. **内部活动不算"活跃"**。在 box 里跑每 5 分钟一次的脉冲（鼠标微动 + CPU + 出网 +
   文件写入），最后一次外部 MCP 调用后约 35 分钟照样休眠。

所以保活进程必须在 box 之外的常开机器上（你的 VPS / 家里服务器都行）。

## 部署三步

```bash
# 0. 把脚本拷到服务器
scp simular_keepalive.py user@server:~/

# 1. 授权（拿服务器专属的独立 token 链，只需一次）
#   （box id 在 dashboard 地址栏或 box_status 返回里，形如 cb-xxxxxxxx）
python3 ~/simular_keepalive.py authorize --port 51777 --box-id cb-xxxxxxxx --wait 1800
#   服务器没浏览器没关系：用你本地浏览器打开它打印的链接点授权，
#   浏览器跳到 localhost:51777 打不开时，把地址栏完整 URL 复制下来，
#   到服务器上 curl '那条完整URL' 即可（code 几分钟内有效，动作要快）。
#   或者用 SSH 隧道：ssh -L 51777:127.0.0.1:51777 user@server，再点链接，自动闭环。

# 2. 装成 systemd 定时任务（每 300 秒一跑）
sudo python3 ~/simular_keepalive.py install

# 3. 验证
tail -f /var/log/simular-keepalive.log   # 应看到 state=running ... ok
```

install 做的事：env 写到 `/etc/simular-keepalive.env`（0600），脚本复制到
`/opt/simular-keepalive/`，注册 `simular-keepalive.service`（oneshot）+ `.timer`。

## 日常命令

```bash
sudo python3 /opt/simular-keepalive/simular_keepalive.py status   # 只看状态，不动机器
sudo python3 /opt/simular-keepalive/simular_keepalive.py run      # 手动跑一次
sudo systemctl disable --now simular-keepalive.timer              # 暂停保活
sudo systemctl enable  --now simular-keepalive.timer              # 恢复
sudo python3 /opt/simular-keepalive/simular_keepalive.py uninstall  # 卸载（env 保留）
```

其他参数：`--interval`（timer 间隔秒，默认 300）、`--box-id`、`--env-file`。

## 机制备忘（改代码前必读）

- **refresh token 单活跃轮换**：每次刷新返回新 rt，旧 rt 立即作废。脚本刷新成功后
  会立刻把新 rt 写回 env 文件 —— 手工备份 env 时注意拿最新版。
- **每个保活端必须独立授权**（跑一次 authorize = 一条独立链）。不要把 Claude Code
  或别的机器的 rt 拿来共用，单活跃轮换会互相踢死。
- 刷新请求必须带 `resource=https://mcp.box.simular.ai/mcp`（RFC 8707），否则签出的
  access token 没有 audience，调接口 401。
- 管理工具在主端点 `https://mcp.box.simular.ai/mcp`，不在 `/mcp/cb-xxx` 分机器端点。
- access token 1 小时过期，脚本缓存到 `/var/lib/simular-keepalive/token.json`，
  快过期才刷新，减少轮换频率。

## 排错

| 现象 | 原因与处理 |
|---|---|
| `HTTP 401` / `invalid_token` | rt 链断了（被另一端刷新踢掉，或 env 是旧备份）。重跑 authorize |
| `refresh 失败 HTTP 400` | rt 已被消费。重跑 authorize 重新授权 |
| `MCP HTTP 4xx` 且刚授权过 | 检查 resource 参数没被去掉 |
| 服务偶发 failed，下一轮自愈 | 平台瞬时故障（实测存在），timer 每 5 分钟重试，无需处理 |
| 日志长时间无新行 | timer 停了：`systemctl status simular-keepalive.timer` |
| 机器仍在休眠 | 确认服务器没关机；`sudo python3 ... run` 手动跑一次看报错 |

## 目录内容

| 文件 | 说明 |
|---|---|
| `simular_keepalive.py` | 保活脚本本体（服务器上部署的就是它） |
