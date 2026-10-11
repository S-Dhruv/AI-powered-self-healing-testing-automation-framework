"""Selenium browser lifecycle. Every navigation is checked against the URL allow-list."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from healtest.security import AllowList, RateLimiter, SecurityError

logger = logging.getLogger(__name__)

_BLUR_JS = """
var sel = 'input[type=password], input[name*=card i], input[name*=cvc i], input[name*=cvv i], input[name*=mail i],'
        + 'input[name*=mobile i], input[name*=phone i], input[autocomplete^=cc]';
document.querySelectorAll(sel).forEach(function (e) { e.dataset.aitfFilter = e.style.filter || ''; e.style.filter = 'blur(7px)'; });
"""
_UNBLUR_JS = """
document.querySelectorAll('[data-aitf-filter]').forEach(function (e) { e.style.filter = e.dataset.aitfFilter; delete e.dataset.aitfFilter; });
"""


class BrowserManager:
    def __init__(self, allow_list: AllowList, limiter: Optional[RateLimiter] = None, headless: bool = False,
                 window_size: str = "1920x1080", page_load_timeout: int = 30,
                 block_hosts: Iterable[str] = (), screenshot_dir: str = "screenshots"):
        self.allow_list, self.limiter = allow_list, limiter
        self.headless, self.window_size, self.page_load_timeout = headless, window_size, page_load_timeout
        self.block_hosts = list(block_hosts)
        self.driver = None
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    def start(self):
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(f"--window-size={self.window_size}")
        if self.block_hosts:
            rules = ", ".join(f"MAP {host} ~NOTFOUND" for host in self.block_hosts)
            options.add_argument(f"--host-resolver-rules={rules}")
        options.set_capability("unhandledPromptBehavior", "ignore")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        except Exception as exc:
            logger.warning("webdriver-manager unavailable (%s); using Selenium Manager", str(exc)[:80])
            service = Service()
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.implicitly_wait(0)
        self.driver.set_page_load_timeout(self.page_load_timeout)
        logger.info("[BROWSER] started (%s)", "headless" if self.headless else "visible window")

    def navigate_to(self, url: str):
        self.allow_list.check(url)
        if self.limiter:
            self.limiter.wait()
        logger.info("[BROWSER] navigate %s", url)
        self.driver.get(url)

    def current_host_allowed(self) -> bool:
        try:
            return self.allow_list.is_allowed(self.driver.current_url)
        except Exception:
            return True

    def take_screenshot(self, prefix: str) -> str:
        try:
            self.driver.execute_script(_BLUR_JS)
            path = self.screenshot_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            self.driver.save_screenshot(str(path))
            self.driver.execute_script(_UNBLUR_JS)
            return str(path)
        except Exception as exc:
            logger.debug("screenshot failed: %s", exc)
            return ""

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("[BROWSER] closed")
            except Exception:
                pass
            self.driver = None
