"""Top3 入库筛选，保持与 PHP PriceStrategyService 的首维度规则一致。"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import json
import re


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def identifiers(values):
    if isinstance(values, str):
        try:
            decoded = json.loads(values)
        except (ValueError, TypeError):
            decoded = None
        values = decoded if isinstance(decoded, (list, dict)) else re.split(r"[\r\n,，、]+", values)
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, list):
        values = [values]
    result = set()
    for value in values:
        if isinstance(value, (list, dict)):
            result.update(identifiers(value))
        elif value is not None:
            text = re.sub(r"\s+", " ", html.unescape(str(value).strip())).strip().lower()
            if text:
                result.add(text)
    return result


def stock_number(item):
    for index, value in enumerate((item.get("stock"), item.get("stock_num"))):
        text = re.sub(r"\s+", "", str(value if value is not None else "")).replace(",", "")
        match = re.fullmatch(r"(\d+(?:\.\d+)?|\.\d+)([kKmMgGbB]?)", text)
        if match:
            amount = number(match[1])
            multiplier = {"k": 1000, "m": 1000000, "g": 1000000000, "b": 1000000000}
            amount *= multiplier.get(match[2].lower(), 1)
            if 0 <= amount <= 2**63 - 1:
                return int(amount)
        # PHP 对规范化 stock_num 额外接受数值（例如科学计数法）。
        if index == 1 and value is not None:
            amount = number(value)
            if amount is not None and 0 <= amount <= 2**63 - 1:
                return int(amount)
    return None


def rating_number(value):
    text = re.sub(r"\s+", "", str(value or "")).replace("%", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", text):
        return None
    rating = number(text)
    return rating if rating is not None and 0 <= rating <= 100 else None


def first_dimension(config):
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError as error:
            raise ValueError("Top3 改价策略 config 不是有效 JSON") from error
    if not isinstance(config, dict):
        raise ValueError("Top3 改价策略 config 必须为对象")
    dimensions = config.get("dimensions", [])
    if isinstance(dimensions, str):
        dimensions = json.loads(dimensions)
    dimension = dimensions[0] if isinstance(dimensions, list) and dimensions else {}
    if not dimension and any(key in config for key in (
        "blacklist_stores", "whitelist_stores", "filter_price", "price",
        "minimum_price", "min_stock", "min_rating",
    )):
        dimension = config
    if isinstance(dimension, str):
        dimension = json.loads(dimension)
    if not isinstance(dimension, dict) or dimension.get("type", "lowest") != "lowest":
        raise ValueError("Top3 仅支持 lowest 首维度策略")
    threshold = next((dimension[key] for key in (
        "filter_price", "price", "minimum_price", "floor_price",
    ) if dimension.get(key) is not None), None)
    min_stock = number(dimension.get("min_stock"))
    min_rating = number(dimension.get("min_rating"))
    return {
        "blacklist": identifiers(dimension.get("blacklist_stores")),
        "whitelist": identifiers(dimension.get("whitelist_stores")),
        "filter_price": number(threshold),
        "min_stock": max(0, int(min_stock)) if min_stock is not None else 0,
        "min_rating": min(Decimal(100), max(Decimal(0), min_rating)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        ) if min_rating is not None else Decimal(0),
    }


def compile_policies(strategies):
    return [(first_dimension(strategy["config"]), strategy.get("currency") or "USD")
            for strategy in strategies]


def eligible(item, policy, currency):
    price = item_price(item)
    if item.get("currency") != currency or price is None or price <= 0:
        return False
    stores = identifiers([item.get("seller_id"), item.get("seller_name")])
    if stores & policy["blacklist"]:
        return False
    if not stores & policy["whitelist"]:
        stock = stock_number(item)
        rating = rating_number(item.get("rating"))
        if policy["min_stock"] > 0 and (stock is None or stock < policy["min_stock"]):
            return False
        if policy["min_rating"] > 0 and (rating is None or rating < policy["min_rating"]):
            return False
    return policy["filter_price"] is None or price > policy["filter_price"]


def item_price(item):
    # 与 db.parse_price 一样接受千位分隔符。
    return number(str(item.get("price", "")).replace(",", ""))


def select_top3(items, policies):
    """每个策略/币种过滤后选最低三条，合并同一条原始竞品并保留原始入库顺序。"""
    selected = set()
    for policy, currency in policies:
        candidates = [index for index, item in enumerate(items) if eligible(item, policy, currency)]
        candidates.sort(key=lambda index: (item_price(items[index]), index))
        selected.update(candidates[:3])
    return [item for index, item in enumerate(items) if index in selected]
