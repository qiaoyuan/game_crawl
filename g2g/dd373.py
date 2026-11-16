"""从 DD373 公布的普通商品比例接口读取用于收货的卖方报价。"""

import asyncio
import re
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from bs4 import BeautifulSoup

from g2g import config
from g2g.dd373_route import dd373_result, resolve_dd373_route


# 官方《查询带比例商品信息》第 3 节，对应有“立即购买”按钮的普通商品。
SHOP_LIST_API = "https://goods.dd373.com/Api/Goods/UserCenter/ApiGetShopList"


def _request_json(url, params):
    with requests.get(
        url, params=params, headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        timeout=(5, 15), allow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise ValueError(f"DD373 接口返回 HTTP {response.status_code}")
        try:
            return response.json(parse_float=Decimal)
        except ValueError as exc:
            raise ValueError("DD373 接口未返回 JSON") from exc


async def _get_json(url, params):
    return await asyncio.to_thread(_request_json, url, params)


def _positive_amount(row, field):
    try:
        value = Decimal(str(row.get(field)))
    except InvalidOperation as exc:
        raise ValueError(f"DD373 商品 {field} 无有效数值") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"DD373 商品 {field} 必须为正数")
    return value


def parse_shop_offers(payload, region):
    rows = dd373_result(payload)
    if not isinstance(rows, list):
        raise ValueError("DD373 缺少商品列表")
    offers = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("unit"), str) or not row["unit"].strip():
            raise ValueError("DD373 商品缺少销售单位")
        lots = _positive_amount(row, "number")
        if lots != lots.to_integral_value():
            raise ValueError("DD373 发布件数必须为整数")
        quantity = _positive_amount(row, "amount")
        stock = lots * quantity
        unit_price = _positive_amount(row, "singleprice")
        line_total = _positive_amount(row, "price")
        offers.append({
            "seller_name": "",
            "deal_rate": None,
            "region": region,
            "stock": stock,
            "sales_unit": "1" + row["unit"].strip(),
            "price": unit_price,
            "currency": "CNY",
            "line_quantity": quantity,
            "line_stock_amount": (lots * line_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "line_total": line_total,
            "offer_url": "",
        })
    return offers


def _parse_deal_rates(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    if re.search(r"登录|安全验证|人机验证|verify|captcha", title, flags=re.I):
        return {}
    if re.search(r"Please slide to verify|请滑动.{0,12}验证", soup.get_text(" ", strip=True), flags=re.I):
        return {}
    rates = {}
    for button in soup.select('div.border-box > div.shop-btn-group[id^="buyButton"]'):
        shopno = button["id"][len("buyButton"):]
        row = button.parent
        if shopno in rates or len(row.select(':scope > div.shop-btn-group[id^="buyButton"]')) != 1:
            rates[shopno] = None
            continue
        labels = [span.get_text(strip=True) for span in row.select("span.font12")
                  if span.find_parent("div", class_="border-box") is row
                  and span.get_text(strip=True).startswith("成交率")]
        rate = None
        if len(labels) == 1:
            match = re.fullmatch(r"成交率\s*[:：]\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*[%％]", labels[0])
            if match:
                value = Decimal(match[1])
                if value <= 100:
                    rate = value
        rates[shopno] = rate
    return rates


def _fetch_deal_rates(url):
    with requests.get(
        url, headers={"Accept": "text/html", "Cache-Control": "no-cache"},
        timeout=(5, 15), allow_redirects=False,
    ) as response:
        if response.status_code != 200 or "text/html" not in response.headers.get("Content-Type", "").lower():
            return {}
        return _parse_deal_rates(response.content)


async def _add_deal_rates(url, source_rows, offers):
    try:
        rates = await asyncio.to_thread(_fetch_deal_rates, url)
        shopnos = [row.get("shopno") if isinstance(row.get("shopno"), str) else None for row in source_rows]
        counts = Counter(shopnos)
        for shopno, offer in zip(shopnos, offers):
            if shopno and counts[shopno] == 1:
                offer["deal_rate"] = rates.get(shopno)
    except Exception as exc:
        print(f"[!] DD373 成交率页面不可用（{type(exc).__name__}），保留 API 报价", flush=True)
        return
    matched = sum(offer["deal_rate"] is not None for offer in offers)
    print(f"[*] DD373 成交率匹配 {matched}/{len(offers)} 条", flush=True)


async def scrape_purchase_offers(url, route_cache=None):
    if route_cache is not None and url in route_cache:
        params, region = route_cache[url]
    else:
        params, region = await resolve_dd373_route(url, _get_json)
        if route_cache is not None:
            route_cache[url] = (params, region)
    payload = await _get_json(SHOP_LIST_API, params)
    offers = parse_shop_offers(payload, region)
    if config.DD373_FETCH_DEAL_RATES and offers:
        await _add_deal_rates(url, dd373_result(payload), offers)
    return offers
