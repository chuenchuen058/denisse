"""Course/week discovery and resource resolution, with no filesystem writes."""
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urljoin, urlsplit

from .dom import Tree


class LoginRequired(RuntimeError):
    pass


@dataclass
class Response:
    url: str
    headers: dict
    html: str = ""


@dataclass(frozen=True)
class Resource:
    module: str
    week: int
    label: str
    url: str
    original: str


def origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.netloc


def allowed_url(url, config):
    origins = [config["browser"]["base_url"], *config["browser"]["extra_download_origins"]]
    if origin(url) not in [origin(v) for v in origins]:
        raise ValueError("Unexpected resource origin: " + url)
    return url


def login_page(response, config):
    c = config["browser"]
    return (bool(re.search(c["login_url"], response.url))
            or bool(list(Tree(response.html).root.select(c["login_dom"]))))


def authenticated(response, config):
    return (origin(response.url) == origin(config["browser"]["base_url"])
            and not login_page(response, config)
            and bool(list(Tree(response.html).root.select(config["browser"]["authenticated_dom"]))))


def scope(html, config):
    tree = Tree(html).root
    mains = list(tree.select(config["scan"]["main"]))
    if not mains:
        raise ValueError("No main content region; refusing an empty successful scan")
    return mains[0]


def filename(url, headers=None):
    message = Message()
    message["content-disposition"] = (headers or {}).get("content-disposition", "")
    value = message.get_filename()
    if isinstance(value, tuple):
        value = collapse_rfc2231_value(value)
    value = value or unquote(PurePosixPath(urlsplit(url).path).name)
    # An original filename is an identity, never a path supplied by a server.
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError("Unsafe original filename: " + str(value))
    return value


def file_links(response, config):
    c = config["scan"]
    for node in scope(response.html, config).select(c["file_nodes"]):
        for attr in c["url_attributes"]:
            url = urljoin(response.url, node.attrs.get(attr, ""))
            if re.search(c["file_url"], url) and not re.search(c["ignore_url"], url):
                yield allowed_url(url, config), node.text()


def resolve(browser, url, label, config):
    c = config["scan"]
    if re.search(c["file_url"], url):
        return [(url, label, filename(url))]
    response = browser.fetch(url)
    if login_page(response, config):
        raise LoginRequired(config["output"]["login_required"])
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in c["html_content_types"]:
        allowed_url(response.url, config)
        if not re.search(c["file_url"], response.url):
            raise ValueError("Resource did not resolve to a content file: " + url)
        return [(response.url, label, filename(response.url, response.headers))]
    links = list(dict.fromkeys(file_links(response, config)))
    if not links:
        raise ValueError("No content file in resource page: " + url)
    if not re.search(c["folder_url"], url):
        links = links[:1]
    return [(target, " ".join((label, text)), filename(target)) for target, text in links]


def discover(browser, manifest, config):
    c = config["scan"]
    seen_resources = set()
    for course, module in manifest["courses"].items():
        url = urljoin(config["browser"]["base_url"], config["browser"]["course"].format(course=course))
        pending, visited, weeks = [url], set(), set()
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            response = browser.fetch(current)
            if login_page(response, config):
                raise LoginRequired(config["output"]["login_required"])
            main = scope(response.html, config)
            sections = list(main.select(c["sections"])) or [main]
            for section in sections:
                headings = list(section.select(c["headings"]))
                match = next((m for h in headings if (m := re.search(c["week_pattern"], h.text()))), None)
                if not match:
                    continue
                week = int(match.group(1))
                weeks.add(week)
                for link in section.select([{"tag": "a"}]):
                    target = urljoin(response.url, link.attrs.get("href", ""))
                    if not (re.search(c["resource_url"], target) or re.search(c["file_url"], target)):
                        continue
                    allowed_url(target, config)
                    key = (module, target)
                    if key in seen_resources:
                        continue
                    seen_resources.add(key)
                    for file_url, label, original in resolve(browser, target, link.text(), config):
                        yield Resource(module, week, label, file_url, original)
            # Moodle's one-section-per-page view exposes week navigation links.
            for link in main.select([{"tag": "a"}]):
                target = urljoin(response.url, link.attrs.get("href", ""))
                if re.search(c["section_url"], target) and re.search(c["week_pattern"], link.text()):
                    pending.append(allowed_url(target, config))
        if not weeks:
            raise ValueError("No Week sections found for course " + str(course))


def eligible(resource, config):
    c = config["scan"]
    suffix = PurePosixPath(resource.original).suffix.lower()
    return (resource.original not in c["skip_original_names"]
            and suffix not in c["skip_extensions"]
            and ("*" in c["take_extensions"] or suffix in c["take_extensions"]))
