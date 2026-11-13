# Загружает проект CryptoPay в открытый репозиторий GitHub "cryptopay".
# Запуск: дважды щёлкните zagruzit-na-github.cmd рядом с этим файлом.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host "== CryptoPay -> GitHub ==" -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Устанавливаю Git..." -ForegroundColor Yellow
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "Устанавливаю GitHub CLI..." -ForegroundColor Yellow
    winget install --id GitHub.cli -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Сейчас откроется браузер. Скопируйте код, который появится ниже, вставьте его на странице GitHub и нажмите Authorize." -ForegroundColor Yellow
    gh auth login --hostname github.com --git-protocol https --web --scopes workflow
    if ($LASTEXITCODE -ne 0) { throw "Вход в GitHub не завершён" }
}
gh auth setup-git | Out-Null

if (-not (Test-Path ".git")) { git init | Out-Null }
git branch -M main
$name = (gh api user --jq .login)
if (-not (git config user.name)) { git config user.name $name }
if (-not (git config user.email)) { git config user.email "$name@users.noreply.github.com" }
git rm -r -q --cached --ignore-unmatch cryptopay-stripe | Out-Null
git add -A
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -q -F commit-message.txt
}

gh repo view "$name/cryptopay" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    gh repo create cryptopay --public --description "CryptoPay: платёжная система для криптокошелька в стиле Stripe (FastAPI, PostgreSQL, MockWallet)" --source . --remote origin --push
} else {
    if (-not (git remote)) { git remote add origin "https://github.com/$name/cryptopay.git" }
    git push -u origin main
}

Write-Host ""
Write-Host "Готово: https://github.com/$name/cryptopay" -ForegroundColor Green
