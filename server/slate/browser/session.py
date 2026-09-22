import os

from playwright.sync_api import sync_playwright


class BrowserSession:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

        self.headless = os.getenv("SLATE_BROWSER_HEADLESS", "true").lower() == "true"

        self.activity = []

    def start(self):
        self.playwright = sync_playwright().start()

        self.browser = self.playwright.chromium.launch(headless=self.headless)

        self.page = self.browser.new_page()

        self._log(
            "browser_started",
            "Started browser session",
        )

    def open(self, url: str):
        if self.page is None:
            self.start()

        self._log(
            "navigate",
            f"Opening {url}",
            {
                "url": url,
            },
        )

        self.page.goto(
            url,
            wait_until="domcontentloaded",
        )

        return self.observe()

    def observe(self):
        if self.page is None:
            raise RuntimeError("Browser session is not started")

        elements = []

        interactive = self.page.locator("a, button, input, textarea, select")

        count = min(interactive.count(), 50)

        for i in range(count):
            element = interactive.nth(i)

            try:
                if not element.is_visible():
                    continue

                tag = element.evaluate("(el) => el.tagName").lower()

                text = ""

                if tag not in ["input", "textarea"]:
                    try:
                        text = element.inner_text().strip()
                    except Exception:
                        pass

                placeholder = element.get_attribute("placeholder")

                aria_label = element.get_attribute("aria-label")

                value = element.get_attribute("value")

                element_type = element.get_attribute("type")

                label = text or aria_label or placeholder or value or tag

                elements.append(
                    {
                        "id": len(elements) + 1,
                        "type": tag,
                        "input_type": element_type,
                        "text": label[:200],
                    }
                )

            except Exception:
                continue

        body_text = ""

        try:
            body_text = self.page.locator("body").inner_text()[:10000]
        except Exception:
            pass

        self._log(
            "observe",
            f"Inspected {len(elements)} interactive elements",
            {
                "element_count": len(elements),
            },
        )

        return {
            "url": self.page.url,
            "title": self.page.title(),
            "elements": elements,
            "text": body_text,
        }

    def act(
        self,
        action: str,
        element_id=None,
        text=None,
        key=None,
    ):
        if self.page is None:
            raise RuntimeError("Browser session is not started")

        if action == "click":
            element = self._element(element_id)

            label = self._element_label(element)

            self._log(
                "click",
                f"Clicked {label}",
                {
                    "element_id": element_id,
                    "element": label,
                },
            )

            element.click()

        elif action == "type":
            element = self._element(element_id)

            label = self._element_label(element)

            # Do NOT log the actual text.
            # This prevents passwords and sensitive
            # information from appearing in activity logs.

            self._log(
                "type",
                f"Typed text into {label}",
                {
                    "element_id": element_id,
                    "element": label,
                    "character_count": len(text or ""),
                },
            )

            element.fill(text or "")

        elif action == "press":
            if element_id is not None:
                element = self._element(element_id)

                label = self._element_label(element)

                self._log(
                    "press",
                    f"Pressed {key} on {label}",
                    {
                        "element_id": element_id,
                        "key": key,
                    },
                )

                element.press(key)

            else:
                self._log(
                    "press",
                    f"Pressed {key}",
                    {
                        "key": key,
                    },
                )

                self.page.keyboard.press(key)

        elif action == "scroll":
            amount = int(text or 500)

            self._log(
                "scroll",
                f"Scrolled {amount}px",
                {
                    "amount": amount,
                },
            )

            self.page.mouse.wheel(
                0,
                amount,
            )

        elif action == "back":
            self._log(
                "navigate",
                "Went back",
            )

            self.page.go_back(wait_until="domcontentloaded")

        elif action == "forward":
            self._log(
                "navigate",
                "Went forward",
            )

            self.page.go_forward(wait_until="domcontentloaded")

        else:
            raise ValueError(f"Unknown browser action: {action}")

        return self.observe()

    def get_activity(self):
        return list(self.activity)

    def clear_activity(self):
        self.activity.clear()

    def _element(self, element_id):
        if element_id is None:
            raise ValueError("element_id is required")

        interactive = self.page.locator("a, button, input, textarea, select")

        visible_index = -1

        for i in range(interactive.count()):
            element = interactive.nth(i)

            try:
                if not element.is_visible():
                    continue

                visible_index += 1

                if visible_index == int(element_id) - 1:
                    return element

            except Exception:
                continue

        raise ValueError(f"Element {element_id} not found")

    def _element_label(self, element):
        try:
            tag = element.evaluate("(el) => el.tagName").lower()

            text = ""

            if tag not in ["input", "textarea"]:
                try:
                    text = element.inner_text().strip()
                except Exception:
                    pass

            aria_label = element.get_attribute("aria-label")

            placeholder = element.get_attribute("placeholder")

            element_type = element.get_attribute("type")

            return (text or aria_label or placeholder or element_type or tag)[:200]

        except Exception:
            return "element"

    def _log(
        self,
        action: str,
        message: str,
        details=None,
    ):
        entry = {
            "action": action,
            "message": message,
            "url": (self.page.url if self.page else None),
            "details": details or {},
        }

        self.activity.append(entry)

        # Keep the activity list bounded.
        if len(self.activity) > 100:
            self.activity.pop(0)

        print(f"[BROWSER] {message}")

    def close(self):
        self._log(
            "browser_closed",
            "Closed browser session",
        )

        if self.browser:
            self.browser.close()

        if self.playwright:
            self.playwright.stop()

        self.browser = None
        self.page = None
        self.playwright = None
