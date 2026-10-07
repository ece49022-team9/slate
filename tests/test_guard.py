import unittest

from slate.agent.guard import review


def no_browser(ref):
    raise AssertionError("browser was not needed")


def page(label, url="https://shop.example/item"):
    def lookup(ref):
        return label, url

    return lookup


class GuardTests(unittest.TestCase):
    def test_message_and_spending_tools_need_approval(self):
        self.assertIn("message", review("send_message", {"target": "Ana"}, no_browser))
        self.assertIn(
            "send", review("mcp__gmail__send_email", {"to": "a@b.c"}, no_browser)
        )
        self.assertIn(
            "money", review("tool_call", {"name": "stripe_create_payment"}, no_browser)
        )

    def test_everyday_tools_and_device_controls_run_without_approval(self):
        for tool in (
            "web_search",
            "read_file",
            "terminal",
            "execute_code",
            "delegate_task",
            "cronjob_manage",
            "mcp__slate_device__execute_device_code",
            "kanban_comment",
        ):
            with self.subTest(tool=tool):
                self.assertIsNone(review(tool, {}, no_browser))

    def test_browser_clicks_are_judged_by_the_element_they_hit(self):
        self.assertIn(
            "spend", review("browser_click", {"ref": "@e4"}, page("Place your order"))
        )
        self.assertIn("spend", review("browser_click", {"ref": "e9"}, page("Buy now")))
        self.assertIn("send", review("browser_click", {"ref": "e2"}, page("Send")))
        for label in ("Search", "Next page", "Add to cart", "Payments help center"):
            with self.subTest(label=label):
                self.assertIsNone(review("browser_click", {"ref": "e1"}, page(label)))

    def test_approval_text_names_the_site_without_query_secrets(self):
        lookup = page("Place order", "https://shop.example/cart?_token=SECRET123")
        message = review("browser_click", {"ref": "e2"}, lookup)
        self.assertIn("place order", message)
        self.assertIn("shop.example/cart", message)
        self.assertNotIn("SECRET123", message)

    def test_enter_on_a_checkout_page_and_card_numbers_need_approval(self):
        checkout = page("", "https://shop.example/checkout/review")
        self.assertIsNotNone(review("browser_press", {"key": "Enter"}, checkout))
        self.assertIsNone(
            review("browser_press", {"key": "Enter"}, page("", "https://a.b/search"))
        )
        self.assertIsNone(review("browser_press", {"key": "Tab"}, checkout))
        self.assertIsNotNone(
            review("browser_type", {"text": "4242 4242 4242 4242"}, no_browser)
        )
        self.assertIsNone(
            review("browser_type", {"text": "weather in Lafayette"}, no_browser)
        )


if __name__ == "__main__":
    unittest.main()
