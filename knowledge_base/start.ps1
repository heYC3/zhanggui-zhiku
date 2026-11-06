# 启动导入、问答和 Listing。解释器优先用 ZHANGGUI_PYTHON。
# 本机路径和代理不要写在这个文件里，放在不提交的 .local.ps1。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$local = Join-Path $PSScriptRoot ".local.ps1"
if (Test-Path $local) { . $local }

if ($env:ZHANGGUI_PYTHON -and (Test-Path $env:ZHANGGUI_PYTHON)) {
    $python = $env:ZHANGGUI_PYTHON
} else {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "找不到 Python。请先运行 .\install.ps1，或设置 ZHANGGUI_PYTHON。"
    }
    $python = $found.Source
}

if (-not $env:HF_HOME) {
    $env:HF_HOME = Join-Path $PSScriptRoot ".cache\huggingface"
}
if (-not $env:MODELSCOPE_CACHE) {
    $env:MODELSCOPE_CACHE = Join-Path $PSScriptRoot ".cache\modelscope"
}
New-Item -ItemType Directory -Force -Path $env:HF_HOME, $env:MODELSCOPE_CACHE | Out-Null

Write-Host "使用解释器：$python"
& $python -m app.server
