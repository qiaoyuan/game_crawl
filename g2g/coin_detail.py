"""仅在能够确认网页 Unit Price 的情况下，采用游戏币详情接口的美元单价。"""

import re

from g2g.coin_offers import CoinApiUnavailable
from g2g.crawl_filter import number


def parse_coin_detail(response, listing) -> str:
    """核对单条公开报价与列表身份，返回详情展示价的原始精度。"""
    if not isinstance(response, dict) or response.get("code") != 2000:
        raise CoinApiUnavailable("游戏币详情请求未成功")
    detail = response.get("payload")
    if not isinstance(detail, dict) or not isinstance(listing, dict):
        raise CoinApiUnavailable("游戏币详情缺少报价数据")

    for key in ("offer_id", "seller_id", "username"):
        reference = listing.get(key)
        if not isinstance(reference, str) or not reference.strip() or detail.get(key) != reference:
            raise CoinApiUnavailable(f"详情与列表身份不匹配：{key}")
    for key in ("region_id", "relation_id", "brand_id", "service_id", "offer_group_attributes", "unit_name", "min_qty"):
        if key in listing and detail.get(key) != listing[key]:
            raise CoinApiUnavailable(f"详情与列表区服或商品属性不匹配：{key}")
    if (detail.get("offer_type") != "public" or detail.get("is_supported_locally") is not True
            or detail.get("status") != "live" or detail.get("is_sold_out") is not False
            or detail.get("is_expired") is not False):
        raise CoinApiUnavailable("详情报价未确认在售")

    # 只有多件起购、库存充足且没有阶梯价时，默认购买数量不会改变此单价。
    minimum = detail.get("min_qty")
    available = detail.get("available_qty")
    if type(minimum) is not int or type(available) is not int or not 1 < minimum <= available:
        raise CoinApiUnavailable("详情购买数量需要网页确认")
    if (type(detail.get("unit_quantity")) is not int or detail["unit_quantity"] != 1
            or detail.get("is_subscription") is not False
            or any(key not in detail or detail[key] is not None
                   for key in ("pricing_packages", "package_settings"))
            or any(detail.get(key) != [] for key in
                   ("wholesale_details", "other_wholesale_details", "other_pricing"))
            or detail.get("cost_wholesale_details")):
        raise CoinApiUnavailable("详情包含未确认的单位、套餐或价格档位")

    currency = detail.get("offer_currency")
    if (detail.get("display_currency") != "USD" or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise CoinApiUnavailable("详情币种未确认")
    price_text = detail.get("display_price")
    if not isinstance(price_text, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", price_text):
        raise CoinApiUnavailable("详情缺少有效的美元展示单价")
    price = number(price_text)
    if price is None or price <= 0 or price != number(detail.get("converted_unit_price")):
        raise CoinApiUnavailable("详情美元展示单价与换算单价不一致")
    if currency == "USD" and price != number(detail.get("unit_price")):
        raise CoinApiUnavailable("详情原币美元单价与展示单价不一致")
    return price_text
