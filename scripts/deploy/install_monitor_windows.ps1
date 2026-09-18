# ============================================================================
# 在 Windows 上注册「外部健康监控」计划任务
#
# 为什么在这台机器上跑：
#   监控必须**独立于被监控对象**。本脚本注册的任务跑在你的 Windows 上，
#   通过 HTTPS 探测远程平台；平台服务器挂了完全不影响这里发告警。
#   （若把监控放在平台服务器上，就会出现"服务挂了、告警也发不出"的自举陷阱。）
#
# 用法（管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File install_monitor_windows.ps1
#   powershell ... -File install_monitor_windows.ps1 -Uninstall
#   powershell ... -File install_monitor_windows.ps1 -IntervalMinutes 10
#
# 前置：
#   1) 已安装 Python（脚本默认用 bee 运行时的 pythonw.exe）
#   2) data/local_config.env 里有 USER_OPEN_ID（告警私信对象）
#   3) 已配置 API Key（写入 data/monitor.env 或环境变量）
# ============================================================================
param(
    [int]$IntervalMinutes = 5,
    [string]$TaskName = "kol-platform-health-monitor",
    [switch]$Uninstall,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

# ---- 路径推导：脚本位于 <skill>/scripts/deploy/ ----
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir  = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$Monitor   = Join-Path $ScriptDir "health_monitor.py"

Write-Host "=== KOL 平台外部健康监控 注册 ===" -ForegroundColor Cyan
Write-Host "  Skill 目录 : $SkillDir"
Write-Host "  监控脚本   : $Monitor"
Write-Host "  计划任务名 : $TaskName"
Write-Host ""

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  ✅ 已移除计划任务 $TaskName" -ForegroundColor Green
    } else {
        Write-Host "  ℹ️  计划任务不存在" -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $Monitor)) {
    Write-Host "  ❌ 找不到监控脚本: $Monitor" -ForegroundColor Red
    exit 1
}

# ---- 定位 pythonw.exe（无控制台窗口，避免每次弹出黑窗）----
$Candidates = @(
    (Join-Path $env:APPDATA "bee_ai_test\agent-runtime\python-venv\Scripts\pythonw.exe"),
    (Join-Path $env:APPDATA "bee_ai_test\agent-runtime\python\pythonw.exe")
)
$Python = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
    Write-Host "  ❌ 未找到 pythonw.exe，请手动指定" -ForegroundColor Red
    exit 1
}
Write-Host "  Python     : $Python"

# ---- 确保 API Key 可用 ----
$EnvFile = Join-Path $SkillDir "data\monitor.env"
if (-not (Test-Path $EnvFile)) {
    $Key = Read-Host "  请输入平台 API Key（留空则跳过业务链路检测）"
    if ($Key) {
        "MONITOR_API_KEY=$Key" | Out-File -FilePath $EnvFile -Encoding ascii -NoNewline
        Write-Host "  ✅ 已写入 $EnvFile" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️  未配置 API Key，「REST 业务链路」检测将失败" -ForegroundColor Yellow
    }
} else {
    Write-Host "  配置       : $EnvFile（已存在）"
}

# ---- 生成启动包装脚本（加载 env 后调用监控）----
$Wrapper = Join-Path $SkillDir "data\_run_health_monitor.cmd"
$envLoad = ""
if (Test-Path $EnvFile) {
    $envLoad = "for /f `"usebackq tokens=1,* delims==`" %%a in (`"$EnvFile`") do set `"%%a=%%b`"`r`n"
}
@"
@echo off
rem 由 install_monitor_windows.ps1 自动生成；外部健康监控入口
rem 说明：用 --quiet 让 Python 只写 UTF-8 文件日志，不输出 stdout。
rem      若在此处 >> 重定向 stdout，cmd 会用 ANSI 编码写文件，
rem      与脚本的 UTF-8 日志混在一起导致乱码（已实测）。
cd /d "$SkillDir"
$envLoad"$Python" "$Monitor" --once --quiet
"@ | Out-File -FilePath $Wrapper -Encoding ascii
Write-Host "  ✅ 已生成包装脚本: $Wrapper" -ForegroundColor Green

# ---- 注册计划任务 ----
$Action = New-ScheduledTaskAction -Execute $Wrapper
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "KOL Skills Platform 外部健康监控（每 $IntervalMinutes 分钟探测一次，异常飞书告警）" | Out-Null

Write-Host "  ✅ 已注册计划任务（每 $IntervalMinutes 分钟）" -ForegroundColor Green
Write-Host ""

if ($RunNow) {
    Write-Host "=== 立即执行一次 ===" -ForegroundColor Cyan
    & $Wrapper
    Write-Host "  已执行，日志见 data\_health_monitor.log"
}

Write-Host ""
Write-Host "常用命令：" -ForegroundColor Cyan
Write-Host "  查看任务    : Get-ScheduledTask -TaskName '$TaskName' | Select TaskName,State"
Write-Host "  立即运行    : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  查看日志    : Get-Content '$SkillDir\data\_health_monitor.log' -Tail 20"
Write-Host "  卸载        : powershell -File `"$($MyInvocation.MyCommand.Path)`" -Uninstall"
Write-Host ""
Write-Host "⚠️  该任务需用户登录后运行（Interactive）。若需免登录，可改用 NSSM 注册为服务。" -ForegroundColor Yellow
