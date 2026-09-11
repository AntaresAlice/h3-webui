<#
  pick_port.ps1 - 为 WebUI 挑选一个可用端口 (Windows)

  用法:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pick_port.ps1 -Start 8080 -Range 40

  输出 (全部走 stdout, run_webui.bat 整段写入临时文件后原样打印):
    # ...            进度日志 (以 # 开头, 解析时忽略)
    RUNNING <port>  该端口上已有 WebUI 在运行, 无需再启动第二个实例
    FREE <port>     该端口可绑定, 可作为监听端口
    NONE 0          候选范围内没有可用端口

  为什么不能只看 netstat: Windows 上 Hyper-V / WSL / winnat 会预留大段
  "排除端口" (本机常见 7981-8080), 落在其中的端口即使没人监听也 bind 不了,
  报 winerror 10013。所以这里用真实 bind 分类, 而不是查询占用列表。

  性能: bind 尝试是瞬时的 (成功或立刻失败), 所以先做一遍 bind 分类;
  只有"不能绑定"的端口才需要再做一次带超时的 HTTP 探测 (判断是否已有 WebUI)。
  整个脚本通常 < 1s, 不会出现长时间静默。
#>
param(
    [string]$BindHost = '127.0.0.1',
    [int]$Start = 8080,
    [int]$Range = 40,
    [string]$StatusPath = '/api/comfyui/status',
    [int]$ConnectTimeoutMs = 300
)

$ErrorActionPreference = 'SilentlyContinue'
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$end = $Start + $Range - 1

$ip = $null
try { $ip = [System.Net.IPAddress]::Parse($BindHost) } catch { $ip = $null }
if (-not $ip) { $ip = [System.Net.IPAddress]::Loopback }

# 进度日志与结果行都走 stdout: run_webui.bat 把整段写进临时文件后原样打印出来,
# 再用 for /f 只挑 RUNNING / FREE / NONE (以 # 开头的进度行会被忽略)
function Log([string]$Message) { Write-Output $Message }

function Test-PortBindable {
    param([int]$Port)
    $listener = New-Object System.Net.Sockets.TcpListener($ip, $Port)
    try {
        $listener.Start()
        $listener.Stop()
        return $true
    } catch {
        try { $listener.Stop() } catch {}
        return $false
    }
}

function Test-WebUiAlive {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($BindHost, $Port)
        if (-not $task.Wait($ConnectTimeoutMs)) { return $false }
        if (-not $client.Connected) { return $false }
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
            -Uri ("http://{0}:{1}{2}" -f $BindHost, $Port, $StatusPath)
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch {}
    }
}

Log "# pick_port: host=$BindHost base=$Start range=$Range"
Log "# step 1/3: bind test on $Start..$end"

$free = @()
$busy = @()
foreach ($p in $Start..$end) {
    if (Test-PortBindable -Port $p) {
        $free += $p
    } else {
        $busy += $p
        Log "#   ${p}: NOT bindable - reserved by Windows or already in use"
    }
}
if ($free.Count -gt 0) {
    Log "#   bindable: $($free.Count)/$Range, first: $($free[0])"
} else {
    Log "#   bindable: 0/$Range"
}

Log "# step 2/3: checking whether a WebUI already answers on a non-bindable port"
foreach ($p in $busy) {
    if (Test-WebUiAlive -Port $p) {
        Log "#   ${p}: answered 200 on $StatusPath - reusing this running WebUI"
        Log "# elapsed: $([int]$sw.ElapsedMilliseconds) ms"
        Write-Output "RUNNING $p"
        exit 0
    }
}
Log "#   no running WebUI found"

Log "# step 3/3: choosing the first bindable port"
if ($free.Count -gt 0) {
    Log "#   chosen: $($free[0])"
    Log "# elapsed: $([int]$sw.ElapsedMilliseconds) ms"
    Write-Output "FREE $($free[0])"
    exit 0
}

Log "#   no bindable port in range"
Log "# elapsed: $([int]$sw.ElapsedMilliseconds) ms"
Write-Output "NONE 0"
exit 0
