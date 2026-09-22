from slate.browser.session import BrowserSession


browser = BrowserSession()

result = browser.open("https://www.google.com")

print(result)

browser.close()