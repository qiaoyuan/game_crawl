"""Local TalkJS fixtures for the sold-order history reader."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError, async_playwright

from g2g.eldorado.shared import eldorado_chat_history as chat


class ChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch()
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.args = SimpleNamespace(messages_frame_selector="iframe", messages_selector="#chat-scrollable")
        self.enterContext(patch.object(chat, "SETTLE_SECONDS", 0.12))
        self.enterContext(patch.object(chat, "POLL_SECONDS", 0.03))

    async def serve(self, html):
        async def route(request):
            body = html if request.request.url.startswith("https://chat.test") else '<iframe src="https://chat.test/frame"></iframe>'
            await request.fulfill(content_type="text/html; charset=utf-8", body=body)
        await self.page.route("**/*", route)
        await self.page.goto("https://order.test/example")

    async def test_waits_for_messages_then_reads_older_rows_including_duplicates(self):
        await self.serve("""
            <style>#chat-scrollable {height:60px; overflow:auto; overflow-anchor:none} .MessageDiv {height:35px}</style>
            <div id="chat-scrollable">Loading…</div>
            <script>
            const box = document.querySelector('#chat-scrollable');
            const row = value => '<div class="MessageDiv"><div class="MessageBody__message-text">' +
                value + '</div></div>';
            let older = true;
            box.onscroll = () => {
                if (older && box.scrollTop === 0) {
                    older = false;
                    setTimeout(() => box.insertAdjacentHTML('afterbegin', row('old') + row('same')), 40);
                }
            };
            setTimeout(() => {box.innerHTML = row('same') + row('same') + row('latest');
                box.scrollTop = box.scrollHeight}, 100);
            </script>
        """)
        snapshot = await chat.read_chat_history(self.page, self.args)
        self.assertEqual([row["text"] for row in snapshot], ["old", "same", "same", "same", "latest"])

    async def test_loading_placeholder_is_not_an_empty_or_successful_conversation(self):
        await self.serve('<div id="chat-scrollable">Loading…</div>')
        with patch.object(chat, "READ_TIMEOUT", 0.4):
            with self.assertRaises(PlaywrightError):
                await chat.read_chat_history(self.page, self.args)
        frame = self.page.frame(url="https://chat.test/frame")
        await frame.locator("#chat-scrollable").evaluate("el => el.textContent = 'No messages yet.'")
        self.assertEqual(await chat.read_chat_history(self.page, self.args), [])

    async def test_reverse_layout_preserves_system_text_and_attachment_only_incoming(self):
        await self.serve("""
            <style>#chat-scrollable {height:65px; display:flex; flex-direction:column-reverse;
                   overflow:auto} .MessageDiv {flex:0 0 45px}</style>
            <div id="chat-scrollable">
              <div class="MessageDiv"><div class="UserMessage__by-other UserMessage__file">
                <div class="MessageBody__message-text"><a class="MessageBody__download-link"
                    href="/signed?secret" download="proof.png">Download file</a></div>
                <a class="test__image-preview" href="/secret"><img alt="proof.png" width="5" height="5"></a>
                <span role="time" title="12:02">now</span></div></div>
              <div class="MessageDiv UserMessage__by-me"><span class="MessageBody__message-text">
                hello</span><span role="time" title="12:01">a minute ago</span></div>
              <div class="MessageDiv">Order created<span role="time" title="12:00">just now</span></div>
            </div>""")
        rows = await chat.read_chat_history(self.page, self.args)
        self.assertEqual([m["time"] for m in rows], ["12:00", "12:01", "12:02"])
        self.assertEqual(rows[0]["text"], "Order created")
        self.assertEqual(rows[0]["source"], "fallback")
        self.assertEqual(rows[2], {"text": "", "time": "12:02", "direction": "incoming",
                                   "attachments": ["proof.png"]})

    def test_virtual_windows_must_overlap_and_keep_distinct_identical_rows(self):
        self.assertEqual(chat._merge([(2, "yes"), (3, "yes")], [(1, "yes"), (2, "yes")]),
                         [(1, "yes"), (2, "yes"), (3, "yes")])
        with self.assertRaises(PlaywrightError):
            chat._merge([(2, "yes")], [(1, "yes")])
