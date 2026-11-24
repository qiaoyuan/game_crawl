"""读取 Eldorado 售出列表当前页中的最新订单或 Aion 2 详情链接。"""

import re
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError


SOLD_URL = "https://www.eldorado.gg/dashboard/orders/sold"
_TIMEOUT_MS = 10000


def _site_path(url):
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc.lower() == "www.eldorado.gg":
        return parts.path.rstrip("/")
    return ""


def is_sold_url(url):
    return _site_path(url) == "/dashboard/orders/sold"


def is_order_url(url):
    return re.fullmatch(r"/order/[A-Za-z0-9_-]+", _site_path(url)) is not None


_SCAN = r"""recent => {
    if (location.origin !== 'https://www.eldorado.gg' ||
        location.pathname.replace(/\/$/, '') !== '/dashboard/orders/sold') return false;
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const readGame = row => {
        const cell = row.querySelector('.game-name');
        if (!cell || !visible(cell)) return '';
        const heading = [...cell.querySelectorAll('h6')].find(el => visible(el) && el.innerText.trim());
        return (heading?.innerText || cell.querySelector('img[alt]')?.alt || cell.innerText).trim();
    };
    const readOrder = row => {
        const link = row.matches('a[href]') ? row : row.querySelector('a[href*="/order/"]');
        if (!link) return null;
        const order = new URL(link.href, location.href);
        const match = /^\/order\/([a-z\d_-]+)\/?$/i.exec(order.pathname);
        if (order.origin !== location.origin || order.username || order.password || !match) return null;
        return {order_id: match[1], url: order.origin + '/order/' + match[1]};
    };

    if (recent) {
        // The dashboard renders the status selector first and the sort selector second.
        const controls = [...document.querySelectorAll('eld-dropdown .dropdown-trigger[role=combobox]')]
            .filter(visible);
        const sort = controls[1];
        if (sort && sort.innerText.trim().toLowerCase() !== 'recent')
            throw new Error('请将售出列表的排序设为 Recent');
    }
    const rows = [...document.querySelectorAll(recent ? 'a.grid-row' : '.grid-row')].filter(visible);
    const empty = /^(?:no (?:sold )?orders(?: (?:found|yet|to show))?|you have no (?:sold )?orders)[.!]?$/im;
    if (!rows.length) return empty.test(document.body.innerText) ? [] : false;
    if (!recent) {
        const urls = new Set();
        let loaded = false;
        for (const row of rows) {
            const game = readGame(row), order = readOrder(row);
            if (!game || !order) continue;
            loaded = true;
            if (/^aion\s*2$/i.test(game)) urls.add(order.url);
        }
        return loaded ? [...urls] : false;
    }

    const readDate = raw => {
        // Parse the English Ordered field in this browser's local timezone.
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const match = /^(\w{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)$/.exec(
            raw.replace(/\s+/g, ' ').trim());
        if (!match) return null;
        const month = months.indexOf(match[1]), day = Number(match[2]), year = Number(match[3]);
        const hour12 = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
        if (month < 0 || hour12 < 1 || hour12 > 12 || minute > 59 || second > 59) return null;
        const hour = hour12 % 12 + (match[7] === 'PM' ? 12 : 0);
        const date = new Date(year, month, day, hour, minute, second);
        if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day ||
            date.getHours() !== hour) return null;
        return date;
    };
    const orders = [];
    for (const row of rows.slice(0, 3)) {
        const order = readOrder(row), game = readGame(row);
        const cell = [...row.querySelectorAll('.date')].find(visible);
        const value = cell?.querySelector('p.font-bold') || cell;
        const raw = value && visible(value) ? value.innerText.trim() : '';
        const date = readDate(raw);
        if (!order || !game || !date) throw new Error('售出列表前三条订单的链接、游戏名或 Ordered 时间无效');
        const epoch = date.getTime() / 1000;
        if (orders.length && orders[orders.length - 1].ordered_at_epoch < epoch)
            throw new Error('售出订单列表未按 Ordered 时间从新到旧排列');
        orders.push({...order, game, ordered_at_raw: raw, ordered_at_epoch: epoch,
                     ordered_at: date.toISOString().replace('.000Z', 'Z')});
    }
    return orders;
}"""


async def _read_sold_list(page, recent):
    if not is_sold_url(page.url):
        raise PlaywrightError("当前页面不是售出订单列表")
    handle = await page.wait_for_function(_SCAN, arg=recent, polling=200, timeout=_TIMEOUT_MS)
    try:
        return await handle.json_value()
    finally:
        await handle.dispose()


async def find_recent_orders(page):
    """返回列表前三行，不限游戏。Ordered 显示时间按浏览器时区解析，再转换为 UTC。"""
    return await _read_sold_list(page, recent=True)


async def find_sold_orders(page):
    """返回当前页全部 Aion 2 订单链接，供全页聊天监控使用。"""
    return await _read_sold_list(page, recent=False)


@asynccontextmanager
async def open_sold_order(list_page, target_url):
    """在列表的登录会话中打开临时详情标签页，验证订单路径并在退出时回收。"""
    if not is_sold_url(list_page.url) or not is_order_url(target_url):
        raise PlaywrightError("售出列表或订单详情链接不匹配")
    detail = await list_page.context.new_page()
    try:
        await detail.goto(target_url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        if _site_path(detail.url) != _site_path(target_url):
            raise PlaywrightError("订单详情发生跳转")
        yield detail
    finally:
        if not detail.is_closed():
            await detail.close()
