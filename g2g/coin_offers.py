"""G2G 游戏币搜索结果与页面竞品卡片之间的转换。"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import quote

from g2g import config
from g2g.crawl_filter import identifiers, number

G2G_COIN_PAGE_SIZE = 20


class CoinApiUnavailable(ValueError):
    """搜索结果不足以安全生成与页面相同的列表。"""


def _numeric(value, field):
    parsed = number(value)
    if parsed is None or parsed < 0 or parsed > 10**24:
        raise CoinApiUnavailable(f"无效的游戏币字段 {field}")
    return parsed


def _short_quantity(value):
    # 页面 formatNumberShort 仅使用 k、M，并向零截断至一位小数。
    amount = _numeric(value, "quantity")
    divisor, suffix = 1, ""
    if amount >= 10**6:
        divisor, suffix = 10**6, "M"
    elif amount >= 1000:
        divisor, suffix = 1000, "k"
    short = (amount / divisor).quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    return format(short, "f").removesuffix(".0") + suffix


def _duration(minutes):
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [(days, "Day"), (hours, "Hr"), (minutes, "Min")]
    parts = parts[:2] if days else parts[1:]
    return " ".join(f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in parts if n)


def _delivery_label(row):
    windows = row.get("delivery_speed_details") or []
    if not windows or row.get("delivery_speed") == "instant":
        return None
    if not isinstance(windows, list) or not all(isinstance(w, dict) for w in windows):
        raise CoinApiUnavailable("无效的游戏币交货时间")
    minutes = [int(_numeric(w.get("delivery_time"), "delivery_time")) for w in windows]
    label = _duration(min(minutes))
    if len(minutes) > 1:
        label += " - " + _duration(max(minutes))
    # DOM 提取只接受带分钟或小时的 badge。
    if "Min" not in label and "Hr" not in label:
        return None
    if row.get("delivery_rate") is not None:
        rate = _numeric(row["delivery_rate"], "delivery_rate") * 100
        label += f" ({rate:.2f}".rstrip("0").rstrip(".") + "%)"
    return label


def _currency_entry(entries):
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise CoinApiUnavailable("无效的游戏币价格列表")
    return next((e for e in entries if e.get("currency") == "USD"), None)


def parse_coin_offers(payload, limit, *, include_listing=False):
    """按页面顺序，在第一页最多 20 个卖家中先排除店铺，再取前 N 个。"""
    if not isinstance(payload, dict) or payload.get("code") != 2000:
        raise CoinApiUnavailable("游戏币搜索未成功")
    body = payload.get("payload")
    rows = body.get("results") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CoinApiUnavailable("游戏币搜索未返回报价列表")

    excluded = identifiers(config.CRAWL_EXCLUDE_STORES)
    items = []
    for row in rows[:G2G_COIN_PAGE_SIZE]:
        if not isinstance(row, dict) or not isinstance(row.get("username"), str):
            raise CoinApiUnavailable("游戏币报价缺少卖家身份")
        name = row["username"].strip()
        if not name:
            raise CoinApiUnavailable("游戏币报价缺少卖家身份")
        # DOM 取卖家链接 pathname 的唯一分段，浏览器在此保留 URL 百分号编码。
        seller_id = quote(name, safe="-._~!$&'()*+,;=:@")
        if excluded & identifiers([seller_id, name]):
            continue
        if row.get("display_currency") != "USD" or row.get("is_group_display"):
            raise CoinApiUnavailable("游戏币报价非 USD 或非独立卖家")

        price = row.get("display_price")
        wholesale = row.get("wholesale_details") or []
        if row.get("offer_currency") != "USD":
            alternative = _currency_entry(row.get("other_pricing") or [])
            if alternative is not None:
                price = alternative.get("unit_price") or price
                tiers = _currency_entry(row.get("other_wholesale_details") or [])
                wholesale = tiers.get("wholesale_details") if tiers else []
        value = _numeric(price, "display_price")
        if value <= 0 or value.adjusted() < -100:
            raise CoinApiUnavailable("游戏币 USD 展示价无效")
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        item = {
            "seller_id": seller_id, "seller_name": name,
            "seller_url": "https://www.g2g.com/" + seller_id,
            "unit_price_raw": text, "currency_label": "USD", "price_label": "From",
            "is_online": row.get("is_online") is True,
        }
        if row.get("user_level") is not None:
            item["seller_level"] = f"Lvl {row['user_level']}"
        if row.get("user_avatar"):
            item["avatar"] = row["user_avatar"]
        if row.get("satisfaction_rate") is not None:
            rate = _numeric(row["satisfaction_rate"], "satisfaction_rate")
            if rate > 1:
                raise CoinApiUnavailable("游戏币好评率超出范围")
            item["rating"] = str((rate * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        if row.get("total_success_order") is not None:
            sold = _numeric(row["total_success_order"], "total_success_order")
            item["sold_count"] = f"{sold:,.0f} sold"
        if row.get("available_qty") is not None:
            item["stock"] = _short_quantity(row["available_qty"])
        if row.get("min_qty") is not None:
            item["min_order"] = "Min. " + _short_quantity(row["min_qty"])
        delivery = _delivery_label(row)
        if delivery:
            item["delivery_time"] = delivery
        if wholesale:
            item["has_volume_discount"] = True
        if include_listing:
            # 仅用于加强时关联同一笔 offer，入库前移除。
            item["_listing"] = row
        items.append(item)
        if len(items) >= limit:
            break
    return items
