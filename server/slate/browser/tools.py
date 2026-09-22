from slate.browser.session import BrowserSession

browser = BrowserSession()


def browser_open(args):
    return browser.open(args["url"])


def browser_observe(args):
    return browser.observe()


def browser_act(args):
    return browser.act(
        action=args["action"],
        element_id=args.get("element_id"),
        text=args.get("text"),
        key=args.get("key"),
    )


def browser_activity(args):
    return browser.get_activity()


def browser_clear_activity(args):
    browser.clear_activity()

    return {
        "success": True,
        "message": "Browser activity cleared",
    }
