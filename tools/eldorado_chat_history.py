"""读取售出订单详情里 TalkJS 已呈现且可向上加载的聊天历史。"""

import asyncio
import re

from playwright.async_api import Error as PlaywrightError


READ_TIMEOUT = 20
SETTLE_SECONDS = 1.2
POLL_SECONDS = 0.2
EMPTY_TEXT = re.compile(r"^(?:no messages(?: yet)?|there are no messages(?: yet)?|暂无消息)[.!。]?$", re.I)


# Inspect the same rendered nodes as extract_messages. Stable IDs (or DOM identity
# within this read) let overlapping scroll windows contain identical messages.
_INSPECT = """([selector, state, move]) => {
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const containers = [...document.querySelectorAll(selector)].filter(visible);
    const nodes = [...new Set(containers.flatMap(el => el.matches('.MessageDiv')
        ? [el] : [...el.querySelectorAll('.MessageDiv')]))].filter(visible);
    const keys = nodes.map(node => {
        if (node.dataset.messageId) return 'message:' + node.dataset.messageId;
        if (!state.ids.has(node)) state.ids.set(node, ++state.counter);
        return state.ids.get(node);
    });
    const order = nodes.map((_, i) => i)
        .sort((a, b) => nodes[a].getBoundingClientRect().top - nodes[b].getBoundingClientRect().top);
    const busy = containers.some(el => el.closest('[aria-busy="true"]') ||
        [...el.querySelectorAll('[aria-busy="true"], [role="progressbar"]')].some(visible));
    let scroller = nodes[0] || containers[0];
    while (scroller && !/(auto|scroll|overlay)/.test(getComputedStyle(scroller).overflowY)) {
        scroller = scroller.parentElement;
    }
    scroller = scroller || document.scrollingElement;
    const reversed = getComputedStyle(scroller).flexDirection === 'column-reverse';
    const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const atTop = reversed ? scroller.scrollTop <= -max + 1 : scroller.scrollTop <= 1;
    if (move === 'latest') scroller.scrollTop = reversed ? 0 : max;
    if (move === 'older') scroller.scrollTop -= Math.max(1, scroller.clientHeight * 0.75);
    return {keys, order, atTop, busy, max,
        emptyText: !nodes.length && containers.length === 1 ? containers[0].innerText.trim() : ''};
}"""


async def extract_messages(frame, selector, *, preserve_lines=False):
    """优先提取 TalkJS 消息字段，不采集附件地址或相对时间。"""
    return await frame.locator(selector).evaluate_all(r"""(containers, preserveLines) => {
        const clean = value => (value || '').replace(/\s+/g, ' ').trim();
        const bodyText = value => preserveLines ? (value || '').trim() : clean(value);
        const visible = node => node.getClientRects().length &&
            getComputedStyle(node).visibility !== 'hidden';
        const nodes = [];
        for (const container of containers.filter(visible)) {
            if (container.matches('.MessageDiv')) nodes.push(container);
            else nodes.push(...container.querySelectorAll('.MessageDiv'));
        }
        const unique = [...new Set(nodes)].filter(visible);
        if (unique.length) return unique.map(node => {
            const body = node.querySelector('.MessageBody__message-text');
            const time = node.querySelector('[role="time"]');
            const marked = sel => node.matches(sel) || node.closest(sel) || node.querySelector(sel);
            const downloadLink = 'a.MessageBody__download-link';
            const preview = 'a.test__image-preview';
            const file = marked('.UserMessage__file');
            const safeLabel = value => {
                const label = clean(value);
                return /(?:https?:\/\/|blob:|data:|^\/\/)/i.test(label) ? '' : label;
            };
            const names = new Set();
            const links = [...node.querySelectorAll('a')].filter(visible).filter(link =>
                link.matches(downloadLink + ',' + preview) || !link.closest('.MessageBody__message-text'));
            for (const link of links) {
                const label = safeLabel(link.getAttribute('download')) || safeLabel(link.innerText);
                if (label) names.add(label);
                for (const img of link.querySelectorAll('img[alt]')) {
                    const alt = safeLabel(img.alt);
                    if (alt && visible(img)) names.add(alt);
                }
            }
            if (!names.size && (file || links.some(link => link.matches(downloadLink + ',' + preview)))) {
                names.add('[附件]');
            }
            // Preserve captions and system text while excluding attachment labels and relative times.
            const scope = body || node;
            const omit = [downloadLink, preview, '[role="time"]'].join(', ');
            let text = bodyText(scope.innerText);
            if (scope.querySelector(omit)) {
                const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
                const parts = [];
                while (walker.nextNode()) {
                    const parent = walker.currentNode.parentElement;
                    if (!parent.closest(omit) && visible(parent)) parts.push(walker.currentNode.data);
                }
                text = clean(parts.join(' '));
            }
            const message = {
                text, time: time ? (time.getAttribute('title') || '') : '',
                direction: marked('.UserMessage__by-me') ? 'outgoing' :
                    marked('.UserMessage__by-other') ? 'incoming' : 'unknown',
                attachments: [...names]
            };
            if (!names.size && !text) message.source = 'unrecognized';
            else if (!body && !names.size) message.source = 'fallback';
            return message;
        });
        // 容器结构改变时保留可见文本，供用户识别；可能包含相对时间。
        return containers.filter(visible).map(node => clean(node.innerText))
            .filter(Boolean).map(text => ({text, source: 'fallback'}));
    }""", preserve_lines)


def _merge(history, window):
    """Combine overlapping ordered runs by identity, rejecting a gap or reused row."""
    if not history:
        return window
    positions = {key: index for index, (key, _) in enumerate(history)}
    for index, (key, _) in enumerate(window):
        if key in positions:
            offset = positions[key] - index
            break
    else:
        raise PlaywrightError("聊天滚动前后没有可核对的共同消息")
    start = max(0, -offset)
    end = min(len(window), len(history) - offset)
    if history[start + offset:end + offset] != window[start:end]:
        raise PlaywrightError("聊天消息在读取期间改变")
    return window[:start] + history + window[end:]


async def _read(page, args):
    url = page.url
    iframes = page.locator(args.messages_frame_selector).locator("visible=true")
    await iframes.first.wait_for(state="visible")
    if await iframes.count() != 1:
        raise PlaywrightError("无法确定订单聊天窗口")
    element = await iframes.first.element_handle()
    frame = await element.content_frame() if element else None
    if not frame:
        raise PlaywrightError("订单聊天窗口仍在加载")
    selector = args.messages_selector
    await frame.locator(selector).first.wait_for(state="visible")
    frame_url = frame.url
    state = await frame.evaluate_handle("() => ({ids: new WeakMap(), counter: 0})")
    try:
        async def inspect(move=None):
            if page.url != url or frame.is_detached() or frame.url != frame_url:
                raise PlaywrightError("订单或聊天页面在读取期间跳转")
            return await frame.evaluate(_INSPECT, [selector, state, move])

        history = []
        stable_since = None
        previous = None
        positioned = False
        while True:
            view = await inspect()
            signature = None
            if not view["busy"]:
                if view["keys"]:
                    if not positioned:
                        await inspect("latest")
                        positioned = True
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    messages = await extract_messages(frame, selector, preserve_lines=True)
                    after = await inspect()
                    if view != after or len(view["keys"]) != len(messages):
                        stable_since = None
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    if len(set(view["keys"])) != len(messages):
                        raise PlaywrightError("聊天消息标识不唯一")
                    history = _merge(history, [(view["keys"][i], messages[i]) for i in view["order"]])
                    signature = (view["keys"], view["max"]) if view["atTop"] else None
                    if not view["atTop"]:
                        await inspect("older")
                elif not history and EMPTY_TEXT.fullmatch(view["emptyText"]):
                    signature = ("empty",)
            now = asyncio.get_running_loop().time()
            if signature is None or signature != previous:
                stable_since = None
            if signature is not None:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= SETTLE_SECONDS:
                    return [message for _, message in history]
            previous = signature
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await state.dispose()


async def read_chat_history(page, args):
    """Return an ordered snapshot or raise a retryable error if it cannot be read."""
    try:
        return await asyncio.wait_for(_read(page, args), timeout=READ_TIMEOUT)
    except asyncio.TimeoutError:
        raise PlaywrightError("聊天历史未在时限内就绪") from None
