"""按详情页显示值读取 Aion 2 售出订单的交付及结算信息。"""

from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from tools.eldorado_sold_list import is_order_url


_TIMEOUT_MS = 10000


_FIELDS = r"""orderId => {
    if (location.origin !== 'https://www.eldorado.gg' ||
        location.pathname.replace(/\/$/, '') !== '/order/' + orderId) return false;
    const visible = el => el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const text = el => el.innerText.replace(/\s+/g, ' ').trim();
    const ids = [...document.querySelectorAll('eld-order-header .order-id span')]
        .filter(visible).map(el => text(el).match(/^Order ID\s*:\s*(\S+)$/i)).filter(Boolean);
    if (!ids.length || ids.some(match => match[1] !== orderId)) return false;

    const fields = {order_id: orderId};
    const readRows = (selector, names) => {
        for (const label of document.querySelectorAll(selector)) {
            if (!visible(label)) continue;
            const key = names.get(text(label).replace(/\s*[:：]$/, '').toLowerCase());
            if (!key) continue;
            const sibling = label.nextElementSibling;
            if (!visible(sibling)) continue;
            const value = text(sibling);
            if (!value) continue;
            if (fields[key] && fields[key] !== value) return false;
            fields[key] = value;
        }
        return [...names.values()].every(key => fields[key]);
    };
    const detailNames = new Map(['game', 'region', 'faction', 'server', 'quantity', 'buyer']
        .map(key => [key, key]));
    const paymentNames = new Map([['order price', 'order_price'], ['commission', 'commission'],
        ['you receive', 'you_receive']]);
    if (!readRows('eld-seller-order-details-card .order-details-card span', detailNames) ||
        !readRows('eld-order-seller-payment .prices-container > div > label', paymentNames) ||
        !/^aion\s*2$/i.test(fields.game)) return false;
    return fields;
}"""


def _matches_order(url, order_id):
    return is_order_url(url) and urlsplit(url).path.rstrip("/") == f"/order/{order_id}"


async def read_order_fields(detail, order_id):
    """等到当前订单的所有必需字段可见后，返回同一份页面快照。"""
    if not _matches_order(detail.url, order_id):
        raise PlaywrightError("订单详情地址与本次订单不符")
    handle = await detail.wait_for_function(
        _FIELDS, arg=order_id, polling=200, timeout=_TIMEOUT_MS)
    try:
        fields = await handle.json_value()
        if not _matches_order(detail.url, order_id):
            raise PlaywrightError("读取期间订单详情发生跳转")
        return fields
    finally:
        await handle.dispose()
