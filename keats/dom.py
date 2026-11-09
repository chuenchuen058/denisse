"""Small HTML tree for configured structural queries (no regex over raw HTML)."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import re


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children).strip()

    def matches(self, queries):
        for query in queries:
            tags = query.get("tag", [])
            tags = [tags] if isinstance(tags, str) else tags
            if tags and self.tag not in tags:
                continue
            if all(re.search(pattern, self.attrs.get(key, ""))
                   for key, pattern in query.get("attrs", {}).items()):
                return True
        return False

    def select(self, queries):
        for child in self.children:
            if isinstance(child, Node):
                if child.matches(queries):
                    yield child
                yield from child.select(queries)


class Tree(HTMLParser):
    # HTML syntax, not site policy: these elements never have closing tags.
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node("")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)
