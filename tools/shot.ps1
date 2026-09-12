# 抓游戏窗口的画面存成 PNG —— 让脚本侧/AI "看得见"游戏现在显示什么。
#
# 为什么需要它: 桥只给得出 scene / inRound 这类**结构化**字段, 菜单光标在哪、
# 哪个按钮高亮, 桥是不知道的。要"帮忙进关卡"这种需要看界面的操作, 就得有画面。
#
# 用 PrintWindow 而不是屏幕截图: 游戏在后台被别的窗口挡住时, 屏幕截图只能拍到
# 挡在前面的窗口; PrintWindow 让窗口自己把自己画一遍, **后台也能拍到内容**。
#
# 用法:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\shot.ps1 [-Out 路径]
param(
    [string]$Out = "$env:TEMP\oc_shot.png",
    [string]$Proc = "Overcooked2"
)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint nFlags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

Add-Type -AssemblyName System.Drawing

$p = Get-Process -Name $Proc -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $p) { Write-Output "ERR 找不到进程 $Proc"; exit 1 }

$h = $p.MainWindowHandle
if ($h -eq [IntPtr]::Zero) { Write-Output "ERR 拿不到主窗口句柄"; exit 1 }

$r = New-Object WinCap+RECT
if (-not [WinCap]::GetWindowRect($h, [ref]$r)) { Write-Output "ERR GetWindowRect 失败"; exit 1 }

$w = $r.Right - $r.Left
$ht = $r.Bottom - $r.Top
if ($w -le 0 -or $ht -le 0) { Write-Output "ERR 窗口尺寸异常 ${w}x${ht}"; exit 1 }

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
# nFlags=2 (PW_RENDERFULLCONTENT) —— 对 DirectX/Unity 窗口, 用 0 常常拍到全黑
$ok = [WinCap]::PrintWindow($h, $hdc, 2)
$g.ReleaseHdc($hdc)
$g.Dispose()

if (-not $ok) { Write-Output "WARN PrintWindow 返回 false (可能拍到黑图)" }

$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output "OK $Out  ${w}x${ht}  visible=$([WinCap]::IsWindowVisible($h))"
