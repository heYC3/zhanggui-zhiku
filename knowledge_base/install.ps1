# 安装完整 RAG 依赖。使用当前 Python，或环境变量 ZHANGGUI_PYTHON。
# 本机路径不要写在这个文件里。需要固定解释器时，放在不提交的 .local.ps1。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$local = Join-Path $PSScriptRoot ".local.ps1"
if (Test-Path $local) { . $local }

if ($env:ZHANGGUI_PYTHON -and (Test-Path $env:ZHANGGUI_PYTHON)) {
    $python = $env:ZHANGGUI_PYTHON
} else {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "找不到 Python。请安装 Python 3.11，或设置 ZHANGGUI_PYTHON。"
    }
    $python = $found.Source
}

Write-Host "使用解释器：$python"
& $python -m pip install --upgrade pip

$torchIndex = "https://download.pytorch.org/whl/cpu"
if ($env:TORCH_INDEX_URL) { $torchIndex = $env:TORCH_INDEX_URL }
Write-Host "安装 CPU 版 PyTorch。"
& $python -m pip install torch torchvision torchaudio --index-url $torchIndex

$torchVersion = & $python -c "import torch; print(torch.__version__)"
if ($torchVersion -notmatch "cpu") {
    throw "装到的不是 CPU 版 PyTorch：$torchVersion"
}
Write-Host "已安装 PyTorch $torchVersion"

$constraint = Join-Path $PSScriptRoot "constraints-cpu.txt"
@(
    "torch==$torchVersion"
    "torchvision"
    "torchaudio"
) | Set-Content -Path $constraint -Encoding ascii

$pipArgs = @(
    "-m", "pip", "install",
    "--upgrade-strategy", "only-if-needed",
    "-c", $constraint,
    "-r", (Join-Path $PSScriptRoot "requirements-rag.txt")
)
if ($env:PIP_INDEX_URL) {
    $pipArgs += @("-i", $env:PIP_INDEX_URL)
    $hostName = ([uri]$env:PIP_INDEX_URL).Host
    if ($hostName) { $pipArgs += @("--trusted-host", $hostName) }
}
Write-Host "安装 RAG 依赖。已有的 CPU 版 PyTorch 不会被换成 GPU 版。"
& $python @pipArgs
if ($LASTEXITCODE -ne 0) { throw "依赖没有装完。" }

& $python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
Write-Host "安装完成。下一步：复制 .env.example 为 .env 并填写，再运行 .\start.ps1"
