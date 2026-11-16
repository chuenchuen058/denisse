"""Chrome session adapter. All authentication is performed by the human in Chrome."""
from contextlib import contextmanager
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import conf
from .scan import LoginRequired, Response, allowed_url, authenticated, login_page


# Fetch only HTML bodies. A force-download response is cancelled immediately after
# headers, so discovery/dry runs do not stream every already-captured PDF or video.
FETCH = """async ({url, timeout, htmlTypes}) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const r = await fetch(url, {credentials: 'include', signal: controller.signal});
    const headers = Object.fromEntries(r.headers.entries());
    if (!r.ok) { if (r.body) await r.body.cancel(); throw new Error(`HTTP ${r.status}`); }
    const type = (headers['content-type'] || '').split(';')[0].toLowerCase();
    let html = '';
    if (htmlTypes.includes(type)) html = await r.text();
    else if (r.body) await r.body.cancel();
    return {url: r.url, headers, html};
  } finally { clearTimeout(timer); }
}"""


class Chrome:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.page = context.pages[0] if context.pages else context.new_page()

    def snapshot(self):
        """The page as it is now. Reading during an SSO redirect fails ('page is navigating');
        wait for the load and read again (browser.snapshot_retries times)."""
        c = self.config["browser"]
        tries = max(1, int(c.get("snapshot_retries", 1)))
        for attempt in range(tries):
            try:
                return Response(self.page.url, {}, self.page.content())
            except Exception:
                if attempt == tries - 1 or self.page.is_closed():
                    raise
                try:
                    self.page.wait_for_load_state(c["wait_until"])
                except Exception:
                    pass
                self.page.wait_for_timeout(c["login_poll_ms"])

    def ensure_login(self, interactive, say=print):
        c = self.config["browser"]
        self.page.goto(urljoin(c["base_url"], c["landing"]), wait_until=c["wait_until"])
        page = self.snapshot()
        if authenticated(page, self.config):
            return
        if not interactive:
            # Mid-redirect (neither signed in nor a login form): let a silent SSO land.
            polls = int(c.get("settle_s", 0) * 1000 // max(1, c["login_poll_ms"]))
            while polls > 0 and not login_page(page, self.config):
                self.page.wait_for_timeout(c["login_poll_ms"])
                page = self.snapshot()
                if authenticated(page, self.config):
                    return
                polls -= 1
            raise LoginRequired(self.config["output"]["login_required"])
        say(self.config["output"]["login_wait"])
        deadline = time.monotonic() + c["login_timeout_s"]
        while time.monotonic() < deadline:
            try:
                self.page.wait_for_timeout(c["login_poll_ms"])
                if authenticated(self.snapshot(), self.config):
                    return
            except Exception:
                # Navigation during an SSO redirect can destroy the execution context, and
                # sign-in can close the tab it started in (16 Nov 2026: 'Target page ... has
                # been closed'). Follow the sign-in to whichever tab is still open.
                if self.page.is_closed():
                    if not self._follow_open_tab():
                        break
        raise LoginRequired(self.config["output"]["login_required"])

    def _follow_open_tab(self):
        """Switch to the newest open tab in this browser; False if none is left."""
        try:
            pages = [p for p in self.context.pages if not p.is_closed()]
        except Exception:
            return False
        if not pages:
            return False
        self.page = pages[-1]
        return True

    def fetch(self, url):
        allowed_url(url, self.config)
        try:
            value = self.page.evaluate(FETCH, {"url": url,
                "timeout": self.config["browser"]["timeout_ms"],
                "htmlTypes": self.config["scan"]["html_content_types"]})
        except Exception:
            # A redirect to an SSO host can be blocked by fetch's CORS rules.
            # Navigate to the landing page to distinguish expiry from a network error.
            self.ensure_login(False)
            raise
        response = Response(**value)
        if login_page(response, self.config):
            raise LoginRequired(self.config["output"]["login_required"])
        return response

    def download(self, url, destination):
        c = self.config["browser"]
        parts = urlsplit(allowed_url(url, self.config))
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(c["force_download"])
        target = urlunsplit(parts._replace(query=urlencode(query)))
        try:
            with self.page.expect_download(timeout=c["download_timeout_ms"]) as event:
                self.page.evaluate("url => { window.location.assign(url); }", target)
            event.value.save_as(destination)
        except Exception:
            if login_page(self.snapshot(), self.config):
                raise LoginRequired(self.config["output"]["login_required"])
            raise


@contextmanager
def session(config, *, login=False, unattended=False, say=print):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(config["output"]["missing_playwright"]) from exc
    profile = conf.profile(config)
    ready = profile / config["paths"]["ready"]
    interactive = login or not ready.exists()
    if interactive and unattended:
        raise LoginRequired(config["output"]["login_required"])
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), channel=config["browser"]["channel"], headless=not interactive,
            accept_downloads=True, timeout=config["browser"]["timeout_ms"])
        try:
            context.set_default_timeout(config["browser"]["timeout_ms"])
            browser = Chrome(context, config)
            browser.ensure_login(interactive, say)
            ready.touch()
            yield browser
        finally:
            context.close()
