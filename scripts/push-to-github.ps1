#requires -Version 5.1
<#
.SYNOPSIS
  Initialize git, sanity-check for leaked secrets, commit, and push to GitHub.

.DESCRIPTION
  This script is defensive: it refuses to push if any tracked file matches a
  real-looking API-key pattern (sk-..., long uuid keys, etc.).

  Steps:
    1. git init (idempotent — skips if .git already exists)
    2. Verify .gitignore correctly excludes server/.env, logs, venv, etc.
    3. Scan all about-to-be-committed files for key-shaped strings
       → abort if anything suspicious is found
    4. Verify git config user.email is set (offers to set it via -GitUserName / -GitUserEmail)
    5. git add . / git commit
    6. git remote add origin <RepoUrl> (idempotent)
    7. git branch -M main
    8. git push -u origin main

.PARAMETER RepoUrl
  Required. Either:
    https://github.com/<user>/<repo>.git    (uses HTTPS; will prompt for PAT)
    git@github.com:<user>/<repo>.git        (uses SSH; assumes ssh key set up)

.PARAMETER GitUserName
  Optional. If git config user.name is unset, this value is set globally.

.PARAMETER GitUserEmail
  Optional. If git config user.email is unset, this value is set globally.

.PARAMETER CommitMessage
  Optional. Defaults to "feat: initial commit (Seecript v0.5)".

.EXAMPLE
  .\scripts\push-to-github.ps1 -RepoUrl https://github.com/yourname/seecript.git

.EXAMPLE
  .\scripts\push-to-github.ps1 `
      -RepoUrl git@github.com:yourname/seecript.git `
      -GitUserName "Your Name" `
      -GitUserEmail "you@example.com"
#>
param(
  [Parameter(Mandatory=$true)]
  [string]$RepoUrl,
  # If set, write these to repo-LOCAL git config (does NOT touch global identity).
  # Recommended for repos that use a different GitHub account than your global zelin19/GitLab one.
  [string]$CommitAuthorName,
  [string]$CommitAuthorEmail,
  # Legacy aliases — still let the user fall back to setting global identity if neither is set.
  [string]$GitUserName,
  [string]$GitUserEmail,
  [string]$CommitMessage = "feat: initial commit (Seecript v0.5)"
)

$ErrorActionPreference = "Stop"

function Step($n, $msg) { Write-Host ("[{0}] {1}" -f $n, $msg) -ForegroundColor Cyan }
function OK($msg)       { Write-Host ("       OK : {0}" -f $msg) -ForegroundColor Green }
function Warn($msg)     { Write-Host ("       WARN : {0}" -f $msg) -ForegroundColor Yellow }
function Die($msg)      { Write-Host ("       FAIL : {0}" -f $msg) -ForegroundColor Red; exit 1 }

# ---- Step 0: project-root guard ----
# Refuse to run if cwd is not the Seecript repo root. This is critical because
# step 3 below runs `git rm -rf --cached .` which would NUKE the staging area of
# whatever git repo happens to own the cwd. We had a real near-miss where this
# got executed inside an unrelated parent project's git repo.
$markers = @("server\app\main.py", "scripts\push-to-github.ps1", "docs\PRD.md")
foreach ($m in $markers) {
  if (-not (Test-Path $m)) {
    Die ("must be run from the Seecript project root (cwd=$PWD). Missing marker: $m`n" +
         "       Hint: cd into the project root first, e.g.`n" +
         "         cd D:\nocode\seecript`n" +
         "         .\scripts\push-to-github.cmd <repo-url> [-CommitAuthor*]")
  }
}
OK "cwd is Seecript root: $PWD"

# ---- Step 1: git init ----
Step "1/8" "git init"
if (Test-Path .git) { OK "already a git repo" } else { git init -q; OK "initialized" }

# ---- Step 2: ensure .gitignore exists and contains the secret excludes ----
Step "2/8" "verify .gitignore covers server/.env, logs/, venv/"
if (-not (Test-Path .gitignore)) { Die ".gitignore not found at repo root" }
$gi = Get-Content .gitignore -Raw
foreach ($must in @("server/.env", "logs", "server/venv", "__pycache__")) {
  if ($gi -notmatch [regex]::Escape($must)) { Die ".gitignore missing rule: $must" }
}
OK ".gitignore looks correct"

# ---- Step 3: refresh staging area + scan for leaked keys ----
Step "3/8" "refresh staging area + scan for leaked secrets"
git rm -rf --cached . -q 2>$null | Out-Null
git add .
$keyRegex = "sk-[A-Fa-f0-9]{20,}|fabcb43c-?2469|DOUBAO_API_KEY=[A-Za-z0-9-]{30,}|DEEPSEEK_API_KEY=sk-[A-Fa-f0-9]{20,}"
$leaks = git diff --cached -G $keyRegex --name-only 2>$null
if ($leaks) {
  Write-Host "       FAIL : detected possible secret leak in:" -ForegroundColor Red
  $leaks | ForEach-Object { Write-Host "                $_" -ForegroundColor Red }
  Write-Host ""
  Write-Host "  Inspect the files above and remove any real key before re-running." -ForegroundColor Yellow
  Write-Host "  Tip: search with Cursor for the matching pattern." -ForegroundColor Yellow
  exit 1
}
OK "no key-shaped strings in tracked files"

# ---- Step 4: ensure git identity is set ----
Step "4/8" "git config user.name / user.email"
if ($CommitAuthorEmail) {
  # Prefer LOCAL config so this repo uses a different identity than the rest of the machine.
  & git config --local user.email $CommitAuthorEmail
  if ($CommitAuthorName) { & git config --local user.name $CommitAuthorName }
  $localName  = (& git config --local user.name)  2>$null
  $localEmail = (& git config --local user.email) 2>$null
  OK "REPO-LOCAL identity set: $localName <$localEmail>"
  OK "Global identity ($((& git config --global user.email))) is untouched."
} else {
  $cfgName  = (& git config --global user.name)  2>$null
  $cfgEmail = (& git config --global user.email) 2>$null
  if (-not $cfgEmail) {
    if (-not $GitUserEmail) {
      Die "git config user.email not set. Re-run with -CommitAuthorEmail (recommended, sets local) or -GitUserEmail (sets global)."
    }
    & git config --global user.email $GitUserEmail
    if ($GitUserName) { & git config --global user.name $GitUserName }
    OK "git identity set globally to $GitUserEmail"
  } else {
    Warn "Using GLOBAL identity: $cfgName <$cfgEmail>"
    Warn "If this is the wrong account (e.g. enterprise email leaking to public GitHub), abort with Ctrl+C and re-run with -CommitAuthorEmail."
    Start-Sleep -Seconds 2
  }
}

# ---- Step 5: commit ----
Step "5/8" "git commit"
$diff = git diff --cached --name-only
if (-not $diff) {
  $hasHead = & git rev-parse --verify HEAD 2>$null
  if ($hasHead) { OK "nothing to commit; working tree clean" }
  else          { Die "nothing staged but no HEAD yet. Did you run git add?" }
} else {
  git commit -m $CommitMessage -q
  OK "committed: $CommitMessage"
}

# ---- Step 6: ensure origin points to RepoUrl ----
# Use `git remote` (lists names, never errors on missing origin) instead of
# `git remote get-url origin` (writes to stderr → trips $ErrorActionPreference=Stop
# even with `2>$null`, which is a well-known PS5.1 / NativeCommandError gotcha).
Step "6/8" "git remote origin -> $RepoUrl"
$remotes = @(& git remote)
if ($remotes -contains "origin") {
  $existing = (& git remote get-url origin).Trim()
  if ($existing -ne $RepoUrl) {
    Warn "remote origin currently = $existing"
    & git remote set-url origin $RepoUrl
    if ($LASTEXITCODE -ne 0) { Die "git remote set-url failed (exit $LASTEXITCODE)" }
    OK "origin updated to $RepoUrl"
  } else {
    OK "origin already set"
  }
} else {
  & git remote add origin $RepoUrl
  if ($LASTEXITCODE -ne 0) { Die "git remote add failed (exit $LASTEXITCODE)" }
  OK "origin added"
}

# ---- Step 7: rename branch to main ----
Step "7/8" "git branch -M main"
& git branch -M main
OK "current branch = main"

# ---- Step 8: push ----
Step "8/8" "git push -u origin main"
Write-Host "  (HTTPS users: GitHub will prompt for username + Personal Access Token)" -ForegroundColor Yellow
Write-Host "  (SSH users  : assumes your ssh key is added to GitHub)" -ForegroundColor Yellow
& git push -u origin main
if ($LASTEXITCODE -ne 0) { Die "push failed (exit $LASTEXITCODE) -- see error above" }
OK "pushed"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  Pushed to: $RepoUrl"                     -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next: open $($RepoUrl -replace '\.git$','') in your browser to verify."
