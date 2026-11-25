"""农历日期换算 — 自包含查表实现（无需第三方依赖）

holidays.yaml 里的农历节日（春节/除夕/端午/中秋）需要换算成公历日期
才能和"今天"比较。

原先的做法是在 YAML 里手工维护 lunar_overrides 表，实测存在两个问题：
  1. 日期填错了 —— 2026 除夕写成 2/17（实际 2/18 才是春节，除夕应为 2/16），
     中秋写成 10/06（实际 2026-09-25），导致节日问候在错误的日子触发
  2. 表只覆盖到 2026 年，之后所有农历节日静默失效

这里改为内置 2024-2040 年三个节日的公历日期表。数据由 .NET
ChineseLunisolarCalendar 导出，并已用 2020-2025 年已知的春节、
2024/2025 年中秋日期验证一致。表外年份会明确提示需要更新，而不是静默出错。
"""
from datetime import date, timedelta

# 春节（农历正月初一）
SPRING_FESTIVAL = {
    2024: "2024-02-10",
    2025: "2025-01-29",
    2026: "2026-02-17",
    2027: "2027-02-06",
    2028: "2028-01-26",
    2029: "2029-02-13",
    2030: "2030-02-03",
    2031: "2031-01-23",
    2032: "2032-02-11",
    2033: "2033-01-31",
    2034: "2034-02-19",
    2035: "2035-02-08",
    2036: "2036-01-28",
    2037: "2037-02-15",
    2038: "2038-02-04",
    2039: "2039-01-24",
    2040: "2040-02-12",
}

# 端午节（农历五月初五）
DRAGON_BOAT = {
    2024: "2024-06-10",
    2025: "2025-05-31",
    2026: "2026-06-19",
    2027: "2027-06-09",
    2028: "2028-06-27",
    2029: "2029-06-16",
    2030: "2030-06-05",
    2031: "2031-06-24",
    2032: "2032-06-12",
    2033: "2033-06-01",
    2034: "2034-06-20",
    2035: "2035-06-10",
    2036: "2036-05-30",
    2037: "2037-06-18",
    2038: "2038-06-07",
    2039: "2039-06-26",
    2040: "2040-06-14",
}

# 中秋节（农历八月十五）
MID_AUTUMN = {
    2024: "2024-09-17",
    2025: "2025-10-06",
    2026: "2026-09-25",
    2027: "2027-09-15",
    2028: "2028-10-03",
    2029: "2029-09-22",
    2030: "2030-09-12",
    2031: "2031-10-01",
    2032: "2032-09-19",
    2033: "2033-09-08",
    2034: "2034-09-27",
    2035: "2035-09-16",
    2036: "2036-10-04",
    2037: "2037-09-24",
    2038: "2038-09-13",
    2039: "2039-10-02",
    2040: "2040-09-20",
}

# (农历月, 农历日) → 数据表
_LUNAR_TABLES = {
    (1, 1): SPRING_FESTIVAL,
    (5, 5): DRAGON_BOAT,
    (8, 15): MID_AUTUMN,
}

_TABLE_MIN_YEAR = min(SPRING_FESTIVAL)
_TABLE_MAX_YEAR = max(SPRING_FESTIVAL)


def _to_date(value):
    """把 "YYYY-MM-DD" 转成 date 对象"""
    year, month, day = (int(part) for part in value.split("-"))
    return date(year, month, day)


def lunar_to_solar(year, lunar_month, lunar_day):
    """农历日期 → 公历 date

    Args:
        year: 公历年份（农历节日落在该公历年内）
        lunar_month: 农历月
        lunar_day: 农历日

    Returns:
        datetime.date，若该组合无数据则返回 None
    """
    table = _LUNAR_TABLES.get((lunar_month, lunar_day))
    if not table:
        return None
    value = table.get(year)
    if not value:
        return None
    return _to_date(value)


def matches(year, month, day, lunar_month, lunar_day, offset_days=0, year_tolerance=1):
    """判断「某年某月某日」是否命中该农历节日（可带偏移天数）

    农历节日与公历年份并非一一对应：2027 年春节在 2027-02-06，
    但它在农历上属于丙午年。因此按 `year` 直接查表可能在年初/年末漏判，
    这里允许在 year ± year_tolerance 范围内查找。

    Args:
        year/month/day: 待判断的公历日期
        lunar_month/lunar_day: 节日的农历月日
        offset_days: 相对节日的偏移，-1 表示节日前一天（除夕）
        year_tolerance: 允许前后查找的年份数

    Returns:
        bool
    """
    for candidate_year in range(year - year_tolerance, year + year_tolerance + 1):
        solar = lunar_to_solar(candidate_year, lunar_month, lunar_day)
        if solar is None:
            continue
        if solar + timedelta(days=offset_days) == date(year, month, day):
            return True
    return False


def table_coverage_warning():
    """表外年份的提示（仅用于日志，返回 None 表示当前年份在覆盖范围内）"""
    today_year = date.today().year
    if today_year > _TABLE_MAX_YEAR:
        return (
            f"农历节日表只覆盖到 {_TABLE_MAX_YEAR} 年，当前是 {today_year} 年，"
            f"农历节日将不会触发。请更新 src/lunar.py 中的数据表。"
        )
    return None
