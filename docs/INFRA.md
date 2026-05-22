# INFRA — 基础设施与域名事实档案

> 本文档是 **唯一权威事实源（single source of truth）**。任何人接手部署前，先看这里。
> 与脚本/README 出现矛盾时，以本文为准并回写。
>
> 最后核实日期：**2026-05-04**（通过 dig + curl + ipapi.co 主动探测确认）

---

## 1. 主域名（已上线）

| 项 | 值 | 来源 / 验证方式 |
|---|---|---|
| 主域名 | `zlhu.asia` | 用户告知 |
| 注册商 | 阿里云万网 | NS 指向 `dns1.hichina.com` / `dns2.hichina.com` |
| DNS 服务商 | 阿里云云解析 DNS（同上） | 在 [dns.console.aliyun.com](https://dns.console.aliyun.com) 管理 |
| 公网 IP（A 记录） | `47.239.58.145` | `Resolve-DnsName zlhu.asia A` |
| 当前 HTTPS 状态 | ✅ 已配证书；HTTP 301 强制跳 HTTPS | `curl -I http://zlhu.asia` |
| 当前 nginx 版本 | nginx/1.18.0 (Ubuntu) | `Server` 响应头 |
| 当前部署内容 | 慢病用药小管家静态 HTML | `<title>慢病用药小管家</title>` |

## 2. 服务器（已确认）

| 项 | 值 | 来源 |
|---|---|---|
| 云厂商 | **阿里云** | IP whois → `Alibaba US Technology Co., Ltd.` (AS45102) |
| 节点 | **香港** | ipapi.co geolocation：`Hong Kong, HK` |
| 推测产品 | 阿里云轻量应用服务器（Lightswitch）香港节点 | 47.239.x.x 段的常见用法 |
| 操作系统 | Ubuntu（具体版本待用户确认，nginx 1.18 暗示 Ubuntu 22.04） | nginx Server 头 |
| ICP 备案 | **不需要** | 香港节点 + .asia 域名，无中国大陆备案要求 |

> ⚠️ **待用户确认（不影响 Seecript 部署，但建议记录）**：
> - 服务器规格（vCPU / RAM / 带宽）
> - 慢病项目部署路径（推测 `/var/www/chronic-medication` 或 `/opt/medi`）
> - 慢病项目运行方式（推测纯静态 nginx，无后端）
> - SSH 用户（root？还是创建了非 root 用户？）
> - SSH 端口（22 还是改了？）

## 3. Seecript 域名规划（待执行）

| 项 | 计划值 | 备注 |
|---|---|---|
| 子域名 | `seecript.zlhu.asia` | 已 dig 确认未占用 |
| A 记录 | 指向 `47.239.58.145`（同主域名） | 阿里云 DNS 控制台添加 |
| TTL | 600s | 首次配置便于调试 |
| HTTPS 证书 | Let's Encrypt（certbot --nginx） | 与主域名独立证书 |
| nginx 内部端口 | `127.0.0.1:5001` | 主项目预留 5000 |
| 项目路径 | `/opt/seecript` | 主项目预留 `/opt/medi` 或 `/var/www/chronic-medication` |
| 运行用户 | `seecript`（脚本自动创建） | 与主项目用户隔离 |
| systemd 单元 | `seecript-server.service` | 与主项目无冲突（主项目纯静态无 systemd） |

## 4. 不会影响主项目的 5 个理由（共存隔离）

| 维度 | 主项目（zlhu.asia） | Seecript（seecript.zlhu.asia） | 隔离强度 |
|---|---|---|---|
| nginx server 块 | 独立 server_name | 独立 server_name | ⭐⭐⭐⭐⭐ |
| TCP 端口 | 80 / 443（公网）| 80 / 443 共享 + 内部 5001 | ⭐⭐⭐⭐ |
| 文件路径 | 不动主项目 root | 独立 `/opt/seecript` | ⭐⭐⭐⭐⭐ |
| 系统用户 | 不动 | 独立 `seecript` 用户 | ⭐⭐⭐⭐⭐ |
| 进程 | 主项目纯静态无进程 | gunicorn + uvicorn workers | ⭐⭐⭐⭐⭐ |

回滚成本：删除 `/etc/nginx/sites-enabled/seecript.conf` + `nginx -s reload` + `systemctl stop/disable seecript-server` 即可彻底卸载，不影响主域名。

## 5. 阿里云控制台操作清单（你需要点的几下）

### 5.1 DNS（必做）

1. 登录 [https://dns.console.aliyun.com/](https://dns.console.aliyun.com/)
2. 找到 `zlhu.asia`，点「解析」
3. 点「添加记录」，填：
   - 记录类型：**A**
   - 主机记录：**`seecript`**
   - 解析线路：默认
   - 记录值：**`47.239.58.145`**
   - TTL：**600 秒**
4. 保存。等 5-10 分钟生效。
5. 本机验证：`Resolve-DnsName seecript.zlhu.asia` 应回 `47.239.58.145`

### 5.2 ECS / 轻量服务器安全组（确认）

1. 登录阿里云轻量应用服务器控制台
2. 找到 zlhu.asia 所在的实例
3. **防火墙 → 入方向**：确保 `80/tcp` 和 `443/tcp` 已放行（应该早就放了，主项目在用）
4. **不需要**额外开 5001 端口（仅 127.0.0.1 监听，不暴露公网）

### 5.3 备案（无需操作）

香港节点 + 海外域名，**无需 ICP 备案**。

## 6. 紧急情况备份与回滚

### 备份（首次部署前必做）

ssh 上服务器后：

```bash
# 备份现有 nginx 配置（按你的规则 B：日期 + 原文件名）
DATE=$(date +%F)
sudo cp -r /etc/nginx /etc/nginx.${DATE}.bak
ls -la /etc/nginx.${DATE}.bak  # 确认备份成功
```

### 回滚（如果 Seecript 部署搞坏了主项目）

```bash
sudo systemctl stop seecript-server
sudo systemctl disable seecript-server
sudo rm -f /etc/systemd/system/seecript-server.service
sudo rm -f /etc/nginx/sites-enabled/seecript.conf
sudo nginx -t && sudo systemctl reload nginx

# 主项目应该立即恢复正常
curl -I https://zlhu.asia
```

如果 nginx 整体坏了：

```bash
sudo systemctl stop nginx
sudo rm -rf /etc/nginx
sudo cp -r /etc/nginx.${DATE}.bak /etc/nginx
sudo nginx -t && sudo systemctl start nginx
```

## 7. 第三方依赖与 API Key

| 服务 | 资源 ID / 端点 | 状态 | 控制台 |
|---|---|---|---|
| DeepSeek LLM | `https://api.deepseek.com/chat/completions` 模型 `deepseek-chat` | Key 已就位 | [platform.deepseek.com](https://platform.deepseek.com/) |
| 火山豆包 ASR **极速版** | 资源 ID `volc.bigasr.auc_turbo`<br>端点 `/api/v3/auc/bigmodel/recognize/flash` | **本地真测已跑通**（2.83s 返回） | [console.volcengine.com/speech/app](https://console.volcengine.com/speech/app) |

> ⚠️ **资源 ID 必须是 `_turbo` 结尾**：`volc.bigasr.auc`（标准版） 与 `volc.bigasr.auc_turbo`（极速版）是两套独立资源，权限不互通。我们在 v0.5 已切换到极速版，因为它支持 base64 inline、不再需要 PUBLIC_BASE_URL/ngrok。

## 8. 变更日志

| 日期 | 变更 | 验证 |
|---|---|---|
| 2026-05-04 | 创建本文档；通过主动探测确认 zlhu.asia 全部基础设施事实 | dig / curl / ipapi.co |
| 2026-05-04 | Seecript 后端从豆包标准版迁移到极速版（v0.5）；本地直调豆包真跑通，移除 ngrok / PUBLIC_BASE_URL / /asr-tmp/ 依赖 | curl POST 565KB wav → 2.83s 返回 transcript |
| (待填) | 添加 A 记录 `seecript.zlhu.asia` | 验证：`dig +short seecript.zlhu.asia` |
| (待填) | 火山控制台开通 `volc.bigasr.auc_turbo` 资源 | 验证：health-check.sh ASR 端点返回 200 |
| (待填) | 安装 Seecript（systemd + nginx） | 验证：`http://seecript.zlhu.asia/api/health` |
| (待填) | 申请 Let's Encrypt 证书 | 验证：`curl -I https://seecript.zlhu.asia` 无证书警告 |

> **每次部署 / 域名变更，回填本表。**
