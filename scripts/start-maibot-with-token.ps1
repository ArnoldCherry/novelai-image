# 运行前先停止已有 MaiBot。本脚本不写入令牌到磁盘或用户环境变量。
$ErrorActionPreference = 'Stop'
$botDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
$startScript = Join-Path $botDirectory 'start.bat'
$secureToken = Read-Host '输入新 NovelAI Persistent API Token（隐藏输入）' -AsSecureString
$tokenPointer = [IntPtr]::Zero
try {
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    $env:NOVELAI_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    if ([string]::IsNullOrWhiteSpace($env:NOVELAI_API_TOKEN)) { throw '令牌不能为空。' }
    & $startScript
    if ($LASTEXITCODE -ne 0) { throw "MaiBot 启动器退出，代码：$LASTEXITCODE" }
} finally {
    Remove-Item Env:NOVELAI_API_TOKEN -ErrorAction SilentlyContinue
    if ($tokenPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer) }
    $secureToken.Dispose()
}
