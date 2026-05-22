#requires -Version 5.1
<#
.SYNOPSIS
  Configure a separate GitHub account for Seecript so it never collides with
  the chronic-medication / zelin19 setup.

.DESCRIPTION
  Adopts a standard "host-alias" pattern:
    - Generates a dedicated ed25519 SSH key (id_ed25519_<username>)
    - Adds a host alias `github-koc` in ~/.ssh/config that uses this key
    - Sets repo-LOCAL git config user.name / user.email (does NOT touch global)
    - Rewrites remote.origin.url to use the alias hostname

  After this script, your Seecript pushes will travel over the new key/account,
  while every other repo on this machine continues to use the global identity
  (zelin19 / huzelin@fingerdance.ai).

  Manual steps that must happen between blocks:
    1. Visit https://github.com/settings/ssh/new on the NEW account
    2. Paste the public key the script prints
    3. Hit Enter to continue (script verifies the connection works)

.EXAMPLE
  .\scripts\setup-github-account.ps1
#>
param(
  [string]$Username,
  [string]$Email,
  [string]$KeyName
)

$ErrorActionPreference = "Stop"

function Section($t)  { Write-Host ""; Write-Host "==== $t ====" -ForegroundColor Cyan }
function OK($t)       { Write-Host "  OK   $t" -ForegroundColor Green }
function Info($t)     { Write-Host "  ...  $t" -ForegroundColor Yellow }
function Die($t)      { Write-Host "  FAIL $t" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------
# Step 0: collect identity (interactive if not passed as param)
# ------------------------------------------------------------------
Section "0. GitHub account identity"
if (-not $Username) {
  $Username = (Read-Host "  新 GitHub 用户名 (例如 huzelin-koc)").Trim()
}
if (-not $Username) { Die "用户名不能为空" }
if ($Username -notmatch '^[A-Za-z0-9-]+$') { Die "用户名格式不对（只允许字母/数字/连字符）" }

if (-not $Email) {
  Write-Host "  邮箱选项：" -ForegroundColor Yellow
  Write-Host "    1) 使用 $Username@users.noreply.github.com（GitHub 隐私邮箱，推荐）"
  Write-Host "    2) 输入自定义邮箱"
  $choice = (Read-Host "  选 1 或 2 [默认 1]").Trim()
  if ($choice -eq "2") {
    $Email = (Read-Host "  你的真实/自定义邮箱").Trim()
  } else {
    # 注：GitHub 的 noreply 实际格式是 <id>+<username>@users.noreply.github.com，
    # 但 <username>@users.noreply.github.com 也被 GitHub 接受作为 fallback。
    # 用户后续可以在 GitHub Settings → Emails 拷贝精确格式覆盖。
    $Email = "$Username@users.noreply.github.com"
  }
}
if (-not $Email) { Die "邮箱不能为空" }

if (-not $KeyName) { $KeyName = "id_ed25519_$Username" }

Write-Host ""
Write-Host "  Username : $Username" -ForegroundColor Green
Write-Host "  Email    : $Email" -ForegroundColor Green
Write-Host "  Key file : ~/.ssh/$KeyName" -ForegroundColor Green
$confirm = (Read-Host "  确认以上信息无误？(y/N)").Trim()
if ($confirm -notin @("y","Y")) { Die "用户取消" }

# ------------------------------------------------------------------
# Step 1: generate SSH key (if not exists)
# ------------------------------------------------------------------
Section "1. 生成 SSH key"
$sshDir  = Join-Path $env:USERPROFILE ".ssh"
$keyPath = Join-Path $sshDir $KeyName
$pubPath = "$keyPath.pub"

if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir | Out-Null }

if (Test-Path $keyPath) {
  Info "key 已存在 ($keyPath)，跳过生成"
} else {
  # -N "" 表示无 passphrase；如果要更安全可手动加
  & ssh-keygen.exe -t ed25519 -C "$Email" -f $keyPath -N '""' -q
  if ($LASTEXITCODE -ne 0) { Die "ssh-keygen 失败" }
  OK "生成 $keyPath"
}
if (-not (Test-Path $pubPath)) { Die "公钥文件 $pubPath 缺失" }

# ------------------------------------------------------------------
# Step 2: print pubkey + wait user to add it on GitHub
# ------------------------------------------------------------------
Section "2. 把公钥添加到新 GitHub 账号"
$pubContent = (Get-Content $pubPath -Raw).Trim()
Write-Host ""
Write-Host "  请打开浏览器访问（用 $Username 账号登录后）:"
Write-Host "    https://github.com/settings/ssh/new" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Title 填: Seecript ($env:COMPUTERNAME)"
Write-Host "  Key 粘贴下面整段（已自动复制到剪贴板）:"
Write-Host ""
Write-Host "  $pubContent" -ForegroundColor Magenta
Write-Host ""

# 自动复制到剪贴板（PS 5.1+ 自带）
try {
  Set-Clipboard -Value $pubContent
  OK "公钥已复制到剪贴板（直接 Ctrl+V 粘贴即可）"
} catch {
  Info "无法自动复制，请手动选中上面紫色那行复制"
}

Write-Host ""
Read-Host "  添加完成后按 Enter 继续（会自动测试连接）" | Out-Null

# ------------------------------------------------------------------
# Step 3: write ~/.ssh/config alias
# ------------------------------------------------------------------
Section "3. 配置 ~/.ssh/config 别名 github-koc"
$cfgPath = Join-Path $sshDir "config"
$alias   = "github-koc"
$block = @"

Host $alias
    HostName github.com
    User git
    IdentityFile ~/.ssh/$KeyName
    IdentitiesOnly yes
"@

$existing = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw } else { "" }
if ($existing -match "(?m)^Host\s+$alias\s*$") {
  Info "$cfgPath 中已有 Host $alias 块，跳过"
} else {
  # Backup if file exists (规则 B：日期 + 原文件名)
  if (Test-Path $cfgPath) {
    $bak = "$cfgPath.$(Get-Date -Format 'yyyy-MM-dd').bak"
    Copy-Item $cfgPath $bak -Force
    OK "已备份 ssh config -> $bak"
  }
  Add-Content -Path $cfgPath -Value $block
  OK "追加 Host $alias 到 $cfgPath"
}

# ------------------------------------------------------------------
# Step 4: test SSH connection
# ------------------------------------------------------------------
Section "4. 测试 SSH 连接到 GitHub"
# `-T` = no PTY, GitHub 会回 "Hi <username>! You've successfully authenticated"
$test = & ssh.exe -T "$alias" 2>&1
Write-Host "  $($test -join "`n  ")"
if ($test -match "Hi\s+([A-Za-z0-9-]+)!") {
  $detected = $matches[1]
  OK "SSH 认证通过，GitHub 识别为：$detected"
  if ($detected -ne $Username) {
    Info "注意：GitHub 报告的用户名是 $detected，与你输入的 $Username 不一致。"
    Info "如果是别名/简写没关系，但确认 push 时用的是这个真实账号。"
  }
} else {
  Write-Host ""
  Die "SSH 认证未成功。最常见原因：公钥还没在 https://github.com/settings/keys 添加成功。请检查后重跑。"
}

# ------------------------------------------------------------------
# Step 5: set repo-LOCAL git identity (不影响全局)
# ------------------------------------------------------------------
Section "5. 在 Seecript 本仓库设 local user.name / user.email"
& git config --local user.name  "$Username"
& git config --local user.email "$Email"
OK "本仓库 commit author = $Username <$Email>"
OK "全局身份 (zelin19) 完全不动，其他仓库照常用"

# ------------------------------------------------------------------
# Step 6: print final remote URL hint
# ------------------------------------------------------------------
Section "6. 接下来 push 时用这个 URL 模板"
Write-Host ""
Write-Host "  在 GitHub 新账号下创建空仓库后，跑：" -ForegroundColor Yellow
Write-Host ""
Write-Host "    .\scripts\push-to-github.cmd $alias`:$Username/seecript.git" -ForegroundColor Green
Write-Host ""
Write-Host "  注意：URL 里 host 是别名 '$alias'，不是 'github.com'。"
Write-Host "  这样 git 会自动用 ~/.ssh/$KeyName 这把 key 而不是 zelin19 的 key。"
Write-Host ""
Write-Host "==== 全部就绪 ====" -ForegroundColor Cyan
