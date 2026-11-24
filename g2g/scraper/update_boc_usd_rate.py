"""手动更新中行美元现汇买入价：python -m tools.update_boc_usd_rate"""

import re
import sys
from decimal import Decimal

import pymysql
import requests
from bs4 import BeautifulSoup

from g2g.db import get_connection


BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
CONFIG_NAME = "usd_cny_rate"


def parse_rate(html: bytes) -> Decimal:
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    table = soup.find("table", id="priceTable")
    if table is None:
        raise ValueError("中行页面缺少外汇牌价表")
    headers = [cell.get_text(strip=True) for cell in table.find_all("th")]
    if "货币名称" not in headers or "现汇买入价" not in headers:
        raise ValueError("中行牌价表缺少货币名称或现汇买入价列")
    currency_column = headers.index("货币名称")
    price_column = headers.index("现汇买入价")
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) <= currency_column or cells[currency_column].get_text(strip=True) != "美元":
            continue
        if len(cells) != len(headers):
            raise ValueError("美元行与表头列数不一致")
        price = cells[price_column].get_text(strip=True)
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", price) or Decimal(price) <= 0:
            raise ValueError("美元现汇买入价无效")
        return Decimal(price) / 100
    raise ValueError("中行牌价表缺少美元行")


def fetch_rate() -> Decimal:
    response = requests.get(BOC_URL, headers={"Cache-Control": "no-cache"}, timeout=20)
    response.raise_for_status()
    return parse_rate(response.content)


def save_rate(rate: Decimal) -> None:
    value = format(rate, "f")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO admin_config
                    (name, title, value, status, type, created_at, updated_at)
                VALUES (%s, '中行美元现汇买入价（人民币/美元）', %s, 1, 0, NOW(), NOW())
                ON DUPLICATE KEY UPDATE value = %s, updated_at = NOW()
                """,
                (CONFIG_NAME, value, value),
            )
        conn.commit()


def main() -> int:
    try:
        rate = fetch_rate()
        save_rate(rate)
    except (requests.RequestException, ValueError, pymysql.MySQLError) as exc:
        print(f"更新美元汇率失败：{exc}", file=sys.stderr)
        return 1
    print(f"已更新 admin_config 中的 {CONFIG_NAME}，1 美元 = {rate:f} 元人民币")
    return 0


if __name__ == "__main__":
    sys.exit(main())
