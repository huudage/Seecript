# Seecript 阿里云部署 SOP

> 给已经做过 Linux 部署的同事看的紧凑版手册。每步只列**做什么**和**怎么验证**。
> 基础设施事实档案见 [INFRA.md](INFRA.md)（注：§7 关于 ASR 资源 ID 的内容已过时，以本文为准 —— 后端 v0.6 起已切回 2.0 标准资源 `volc.bigasr.auc`）。
>
> 最后更新：2026-05-30

---

## 0. 前置确认

| 项 | 你需要准备好 |
|---|---|
| 阿里云 ECS / 轻量 | Ubuntu 22.04 LTS，建议 ≥ 2 vCPU / 4 GB RAM / 40 GB 盘（ffmpeg 转码 + AIGC 缓存吃盘） |
| 公网 IP | 服务器有固定公网 IP |
| 后端端口 | 默认 5001；**先 `ss -tlnp \| grep :5001` 确认空闲**。该机已有 kocopilot 占 5001 时改用 5002（同步改 `.env` 的 `PORT=`、systemd unit 的 `--bind`、nginx 的 `proxy_pass`，共 3 处） |
| 安全组 / 防火墙 | `80/tcp` `443/tcp` 已放行；不需要开 5001 |
| 域名 | 已注册并指向阿里云 DNS；本例 `seecript.zlhu.asia` |
| ICP 备案 | 中国大陆节点 → 已备案；香港 / 海外节点 → 不需要 |
| 火山方舟 Key×2 | `ARK_API_KEY`（LLM + 备用 T2V）+ `ARK_T2V_API_KEY`（Seedance 独立计费，可选） |
| 豆包 ASR Key | `DOUBAO_API_KEY`（录音文件识别 2.0；和 ARK 是**不同**的应用） |
| TTS（可暂缓） | 火山「语音技术 → 语音合成」的 App ID + Access Token；首次部署可留 mock |
| 本机网速 | 上传项目 tar 包约 30-50 MB，scp 几分钟 |

---

## 1. 控制台一次性配置

### 1.1 DNS

阿里云云解析 DNS（[dns.console.aliyun.com](https://dns.console.aliyun.com)）→ 选 `zlhu.asia` → 添加记录：

- 记录类型：`A`
- 主机记录：`seecript`
- 记录值：`<你的服务器公网 IP>`
- TTL：`600`

验证：

```bash
dig +short seecript.zlhu.asia
# 应返回你服务器的公网 IP
```

### 1.2 安全组 / 防火墙

放行 `80/tcp` + `443/tcp`。**不要**对外暴露 5001（仅 127.0.0.1）。

### 1.3 火山方舟（ARK）

1. 控制台 [console.volcengine.com/ark](https://console.volcengine.com/ark) → API Key 管理 → 创建 Key（这是 `ARK_API_KEY`）
2. 模型推理 → 在线推理点 → 开通 `doubao-seed-2-0-lite`（LLM）
3. 视频生成 → 开通 `doubao-seedance-2-0-fast-260128`（Seedance T2V）
4. **可选**：如果 Seedance 要走独立计费，再创建一个 Key 作 `ARK_T2V_API_KEY`；不需要就把这个字段留空，T2V client 会自动复用 `ARK_API_KEY`

### 1.4 豆包 ASR 2.0

1. 控制台「语音技术 → 录音文件识别 2.0」→ 创建应用，拿到 `DOUBAO_API_KEY`
2. **资源 ID = `volc.bigasr.auc`**（标准 2.0），**不是**旧极速版 `volc.bigasr.auc_turbo`
3. **域名白名单**：进应用配置 → 加 `seecript.zlhu.asia`（火山服务端会通过该域名回拉 `/uploads/...` 音频）

### 1.5 TTS（首次部署可跳过）

留 mock 先把链路跑通。等申请到后：

1. 控制台「语音技术 → 语音合成」→ 创建应用，勾选音色 `zh_female_qingxin`
2. 拿 App ID 和 Access Token，写入 `.env` 的 `VOLC_TTS_APP_ID` / `VOLC_TTS_ACCESS_TOKEN`
3. 把 `TTS_PROVIDER=mock` 改成 `TTS_PROVIDER=volc`
4. `sudo systemctl restart seecript-server`

---

## 2. 上传代码

本机 PowerShell（不用 git）：

```powershell
# 在项目根 D:\Seecript
$tar = "seecript-deploy-$(Get-Date -Format yyyyMMdd-HHmm).tar.gz"
tar --exclude='.git' --exclude='node_modules' --exclude='web/dist' `
    --exclude='server/venv' --exclude='server/var' `
    --exclude='server/logs' --exclude='server/.env' `
    -czf $tar .
scp $tar root@<server-ip>:/tmp/
```

服务器侧解压：

```bash
sudo mkdir -p /opt/seecript
sudo tar -xzf /tmp/seecript-deploy-*.tar.gz -C /opt/seecript
sudo chown -R root:root /opt/seecript  # install 脚本会 chown 到 seecript 用户
ls /opt/seecript/scripts/install-on-medi-server.sh  # 验证 sentinel 文件存在
```

---

## 3. 跑安装脚本

```bash
sudo bash /opt/seecript/scripts/install-on-medi-server.sh
```

脚本会：

- 装 ffmpeg / nginx / certbot / Node 20 / python3-venv 等系统依赖
- 建 `seecript` 系统用户 + `/opt/seecript` 目录权限
- 建 Python venv + pip install（含 gunicorn）
- 执行 `npm ci && npm run build`，输出 `web/dist`
- 交互式提示 Key，写到 `/opt/seecript/server/.env`（chmod 600）
- 配 systemd unit + nginx site
- 调 `/api/health` 自检

Key 提示出现时直接粘贴，不在本文档里留痕。

如果中途任何一步失败：

```bash
journalctl -u seecript-server -n 100      # 后端起不来
sudo nginx -t                              # nginx 配错
sudo -u seecript cat /opt/seecript/server/.env | grep -v API_KEY  # 看脱敏配置
```

---

## 4. 签 HTTPS 证书

DNS 已生效后：

```bash
sudo certbot --nginx -d seecript.zlhu.asia
# 选项 2 - 强制 HTTP → HTTPS 跳转
```

certbot 会自动在 `/etc/nginx/sites-available/seecript.conf` 里插入 443 server 块和 HTTP 301。

验证：

```bash
curl -I https://seecript.zlhu.asia        # 200 + 你的 HTML
curl -I http://seecript.zlhu.asia         # 301 → https
curl https://seecript.zlhu.asia/api/health  # {"status":"ok"}
```

---

## 5. 端到端冒烟（必跑）

```bash
sudo bash /opt/seecript/scripts/health-check.sh https://seecript.zlhu.asia
```

如果项目里有 health-check.sh，它会依次 ping：`/api/health`、`/samples/...`、`/api/library` 等。
没有也没关系，手工验证：

| 检查 | 命令 | 期望 |
|---|---|---|
| 前端静态 | `curl -I https://seecript.zlhu.asia/` | 200 + `text/html` |
| Vite 哈希 bundle | `curl -I https://seecript.zlhu.asia/static/` 任意 hashed 文件 | 200 + `Cache-Control: immutable` |
| 后端健康 | `curl https://seecript.zlhu.asia/api/health` | `{"status":"ok"}` |
| 样例库 | `curl https://seecript.zlhu.asia/api/library` | `[{...samples...}]` |
| 样例视频回源 | `curl -I https://seecript.zlhu.asia/samples/<id>/raw.mp4` | 200 + `video/mp4` |
| COOP/COEP（ffmpeg.wasm 必需） | `curl -I https://seecript.zlhu.asia/` | 看到两个 `Cross-Origin-*-Policy` 响应头 |

浏览器手测：

1. 打开 `https://seecript.zlhu.asia/`
2. F12 → Console 不应有 `cross-origin isolation` 报错
3. 选一个样例 → Decompose → 上传一段 30s 视频 → 看是否能跑到 Compose 出 plan
4. 真实场景验证 ASR：上传带人声的视频，看 `journalctl -u seecript-server -f` 是否记录 `[asr] submit ok` + `[asr] query done` 而不是 mock 输出

---

## 6. 常见问题排查

### `gunicorn: command not found`

install 脚本会跑 `pip install gunicorn`，如果跳过了：

```bash
sudo -u seecript /opt/seecript/server/venv/bin/pip install gunicorn
sudo systemctl restart seecript-server
```

### ASR 调用 403 / 资源不可用

- 控制台核对资源 ID 是 `volc.bigasr.auc` 而非 `_turbo`
- 应用「域名白名单」里加了 `seecript.zlhu.asia`
- `.env` 里 `PUBLIC_AUDIO_BASE_URL=https://seecript.zlhu.asia`（不带尾斜线，不含路径）
- 火山服务端从公网拉 `/uploads/<file>` 必须能 200——`curl -I https://seecript.zlhu.asia/uploads/<某文件>` 测一下

### `/render` 时 504 Gateway Timeout

`fill_gap`（Seedance T2V）链路化偶尔 60s+。当前 nginx 兜底 180s、gunicorn 兜底 120s。如果常 504：

```bash
# 编辑 systemd unit
sudo systemctl edit seecript-server --full
# 把 --timeout 120 改成 240，保存退出
sudo systemctl daemon-reload && sudo systemctl restart seecript-server

# 编辑 nginx site
sudo vi /etc/nginx/sites-enabled/seecript.conf
# 把 /api/ 块里 proxy_*_timeout 180s 改成 240s
sudo nginx -t && sudo systemctl reload nginx
```

### 切到真 TTS 后无声 / 报 401

- `TTS_PROVIDER=volc`
- App ID 是短数字串；Access Token 是长串（不要混了）
- `VOLC_TTS_CLUSTER=volcano_tts` 默认值通常正确，控制台应用类型若是「中文（标准音色）」必须用 `volcano_tts`
- 应用必须勾选了对应音色（如 `zh_female_qingxin`），不勾选会被静默拒绝

### Cross-Origin Isolation 报错

浏览器报 `ffmpeg.wasm requires cross-origin isolation`：

```bash
curl -I https://seecript.zlhu.asia/ | grep -i cross-origin
# 必须两个都有：
# Cross-Origin-Opener-Policy: same-origin
# Cross-Origin-Embedder-Policy: credentialless
```

少了 → 检查 nginx 配是不是被覆盖了（`add_header` 在 location 里有任何 `add_header` 都要再加一遍，否则父块不继承）。

### 上传项目后 `npm ci` 失败

Node 太老。Ubuntu 22.04 默认 `apt install nodejs` 是 v12.x，跑不了 Vite 8。install 脚本里有 NodeSource 20.x 引导，如果你跳过了：

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs
node -v  # v20.x.x
```

---

## 7. 卸载 / 回滚

```bash
sudo systemctl stop seecript-server
sudo systemctl disable seecript-server
sudo rm -f /etc/systemd/system/seecript-server.service
sudo rm -f /etc/nginx/sites-enabled/seecript.conf
sudo rm -f /etc/sudoers.d/seecript
sudo nginx -t && sudo systemctl reload nginx
sudo rm -rf /opt/seecript     # 含用户上传 / 渲染产物，不可逆，慎重
sudo userdel -r seecript      # 删用户和 home
```

主项目 `https://zlhu.asia` 不受影响。

---

## 8. 变更日志

| 日期 | 变更 | 验证 |
|---|---|---|
| 2026-05-30 | 创建本 SOP，作为部署唯一权威；澄清 ASR 已回归 2.0 标准资源 | 本地 dry-run install 脚本 |
| (待填) | 阿里云首次部署完成 | 端到端冒烟 §5 全绿 |
| (待填) | 申请 TTS 应用 + 写入 .env | `journalctl -u seecript-server` 看到 `[tts] volc synth ok` |
