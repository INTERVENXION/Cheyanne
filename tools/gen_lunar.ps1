# 农历节日表生成脚本
#
# src/lunar.py 里的春节 / 端午 / 中秋日期表由本脚本生成。
# 数据源是 .NET 的 ChineseLunisolarCalendar（Windows 内置），
# 已用 2020-2025 年已知春节日期和 2024/2025 年中秋日期验证一致。
#
# 用法（项目根目录）：
#     powershell -ExecutionPolicy Bypass -File tools/gen_lunar.ps1
#     powershell -ExecutionPolicy Bypass -File tools/gen_lunar.ps1 -Check
#
# 把输出的三个字典粘贴回 src/lunar.py。
#
# 注意：本文件含中文，必须以 UTF-8 BOM 保存，
# 否则 Windows PowerShell 5.1 会按 ANSI 解析导致中文乱码。

param(
    [int]$From = 2024,
    [int]$To = 2040,
    [switch]$Check
)

$c = [System.Globalization.ChineseLunisolarCalendar]::new()
$spring = @{}
$dragon = @{}
$mid = @{}

foreach ($y in $From..$To) {
    foreach ($m in 1..12) {
        $dim = [DateTime]::DaysInMonth($y, $m)
        foreach ($d in 1..$dim) {
            $dt = Get-Date -Year $y -Month $m -Day $d
            $ly = $c.GetYear($dt)
            $lm = $c.GetMonth($dt)
            $ld = $c.GetDayOfMonth($dt)
            $leap = $c.GetLeapMonth($ly)
            $lmAdj = if ($leap -gt 0 -and $lm -ge $leap) { $lm - 1 } else { $lm }

            if ($lmAdj -eq 1 -and $ld -eq 1) { $spring[$ly] = $dt.ToString('yyyy-MM-dd') }
            elseif ($lmAdj -eq 5 -and $ld -eq 5) { $dragon[$ly] = $dt.ToString('yyyy-MM-dd') }
            elseif ($lmAdj -eq 8 -and $ld -eq 15) { $mid[$ly] = $dt.ToString('yyyy-MM-dd') }
        }
    }
}

function Format-LunarRows {
    param([hashtable]$Table)
    $Table.Keys | Sort-Object | ForEach-Object {
        '    {0}: "{1}",' -f $_, $Table[$_]
    }
}

if ($Check) {
    $root = Split-Path -Parent $PSScriptRoot
    $src = Join-Path $root 'src\lunar.py'
    if (-not (Test-Path $src)) { Write-Error "cannot find $src"; exit 2 }
    $generated = @()
    $generated += Format-LunarRows $spring
    $generated += Format-LunarRows $dragon
    $generated += Format-LunarRows $mid
    $existing = Get-Content $src -Encoding UTF8 | Where-Object { $_ -match '^\s+\d{4}: "' }
    $diff = Compare-Object $generated $existing
    if ($diff) {
        Write-Output 'FAIL: src/lunar.py table differs from generated output'
        $diff | Out-String | Write-Output
        exit 1
    }
    Write-Output "OK: src/lunar.py table matches generated output ($($existing.Count) entries)"
    exit 0
}

Write-Output "# Spring Festival (lunar month 1, day 1)"
Write-Output "SPRING_FESTIVAL = {"
Format-LunarRows $spring | Write-Output
Write-Output "}"
Write-Output ""
Write-Output "# Dragon Boat Festival (lunar month 5, day 5)"
Write-Output "DRAGON_BOAT = {"
Format-LunarRows $dragon | Write-Output
Write-Output "}"
Write-Output ""
Write-Output "# Mid-Autumn Festival (lunar month 8, day 15)"
Write-Output "MID_AUTUMN = {"
Format-LunarRows $mid | Write-Output
Write-Output "}"