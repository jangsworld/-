"""글씨 편집기(굵게·기울임·색·크기)로 쓴 글을 안전하게 다루기.

화면에서 온 글은 그대로 믿지 않는다. 허용한 태그와 글자 모양만 남기고 나머지(스크립트, 링크, 그림, 이벤트 속성)는 버린다.
저장할 때와 화면에 다시 보여 줄 때 모두 clean() 을 거친다.
"""
import re
from html import escape, unescape
from html.parser import HTMLParser

TAGS = {"b", "strong", "i", "em", "u", "s", "span", "font", "br", "p", "div", "ul", "ol", "li"}
VOID = {"br"}
DROP = {"script", "style", "iframe", "object", "template", "svg", "math", "title", "noscript", "textarea", "select"}  # 안의 글까지 버림
STYLES = {"color", "background-color", "font-size", "font-weight", "font-style", "text-decoration", "text-decoration-line", "text-align"}
_VALUE = re.compile(r"^[#a-zA-Z0-9 ,.()%-]{1,60}$")
_COLOR = re.compile(r"^(#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20})$")
_BAD = ("url", "expression", "var(", "attr(", "image", "import")
HEAD_COLOR = "#1f4e79"


def _style(value: str) -> str:
    out = []
    for part in value.split(";"):
        k, _, v = part.partition(":")
        k, v = k.strip().lower(), v.strip()
        if k in STYLES and _VALUE.match(v) and not any(b in v.lower() for b in _BAD):
            out.append(f"{k}: {v}")
    return "; ".join(out)


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP:
            self.skip += 1
            return
        if self.skip or tag not in TAGS:
            return
        a = ""
        for k, v in attrs:
            v = v or ""
            if k == "style":
                st = _style(v)
                if st:
                    a += f' style="{escape(st)}"'
            elif tag == "font" and k == "color" and _COLOR.match(v):
                a += f' color="{v}"'
            elif tag == "font" and k == "size" and re.fullmatch(r"[1-7]", v):
                a += f' size="{v}"'
        self.out.append(f"<{tag}{a}>")
        if tag not in VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in DROP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag in VOID or tag not in self.open:
            return
        while self.open:
            t = self.open.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        self.close()
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def clean(html: str) -> str:
    """허용한 태그·모양만 남긴 HTML."""
    p = _Cleaner()
    p.feed((html or "").replace("\x00", ""))
    return p.result().strip()


def to_text(html: str) -> str:
    """메일의 글자만 있는 본문·빈 글 확인용."""
    s = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    s = re.sub(r"(?i)<li[^>]*>", " - ", s)
    s = re.sub(r"(?i)</(p|div|li|ul|ol)>", "\n", s)
    s = unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def from_text(text: str) -> str:
    """글자만 있는 글 → 편집기용 HTML. '1. 제목' 줄은 굵은 색 제목, ' - ' 줄은 목록으로."""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            items.clear()

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("- "):
            items.append(escape(s[2:], quote=False))
            continue
        flush()
        if not s:
            out.append("<div><br></div>")
        elif re.match(r"^\d{1,2}\.\s", s):
            out.append(f'<div><b><span style="color: {HEAD_COLOR}; font-size: large">{escape(s, quote=False)}</span></b></div>')
        else:
            out.append(f"<div>{escape(s, quote=False)}</div>")
    flush()
    return "".join(out)
