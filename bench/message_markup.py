"""Compare parsed message markup on the paired disposable fixtures."""

from html.parser import HTMLParser
import re


class MessageMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = []
        self.csrf_values = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "authenticity_token":
            self.csrf_values.append(attributes.get("value"))
            attributes["value"] = "<csrf>"
        if tag == "img" and (attributes.get("src") or "").startswith("/users/"):
            attributes["src"] = "<signed-avatar>"
        if "data-copy-to-clipboard-content-value" in attributes:
            attributes["data-copy-to-clipboard-content-value"] = re.sub(
                r"^https?://[^/]+", "<origin>", attributes["data-copy-to-clipboard-content-value"]
            )
        if attributes.get("title") in ("Test Admin", "User 1"):
            attributes["title"] = "<creator>"
        self.events.append(("start", tag, tuple(sorted(attributes.items()))))

    def handle_endtag(self, tag):
        if tag not in ("img", "input"):
            self.events.append(("end", tag))

    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            if value in ("Test Admin", "User 1"):
                value = "<creator>"
            elif value in ("All Talk", "Campfire"):
                value = "<room>"
            self.events.append(("text", value))


def check_message_markup(camp_body, rust_body, messages):
    camp, rust = MessageMarkup(), MessageMarkup()
    camp.feed(camp_body.decode())
    rust.feed(rust_body.decode())
    assert len(camp.csrf_values) == len(rust.csrf_values) == messages * 8, (len(camp.csrf_values), len(rust.csrf_values))
    assert all(camp.csrf_values) and set(rust.csrf_values) == {"benchmark-csrf"}
    assert camp.events == rust.events, next(
        ((index, left, right) for index, (left, right) in enumerate(zip(camp.events, rust.events)) if left != right),
        (len(camp.events), len(rust.events)),
    )
