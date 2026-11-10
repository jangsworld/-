"""한글 문서(.hwp 5.x 바이너리, .hwpx) → 문단·표 구조.

PDF·스캔처럼 글자를 '판독'하지 않고 문서 안의 글자를 그대로 읽으므로 오타가 없고,
문단 정렬(가운데·오른쪽), 굵게, 글자 크기, 표(병합 포함)를 살립니다. 그림·도형·머리말 등은 건너뜁니다.

결과 블록:
  {"t": "p", "runs": [(글, 굵게)], "align": "left|center|right|justify", "size": pt}
  {"t": "table", "rows": [[{"colspan", "rowspan", "blocks": [...]}]]}
참고: 한글 문서 파일 형식 5.0 공개 문서(한글과컴퓨터).
"""
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

# HWP 5 레코드 태그 (HWPTAG_BEGIN = 0x10)
T_CHAR_SHAPE, T_PARA_SHAPE = 21, 25
T_PARA_HEADER, T_PARA_TEXT, T_PARA_CHAR_SHAPE = 66, 67, 68
T_CTRL_HEADER, T_LIST_HEADER, T_TABLE = 71, 72, 77
ALIGN = {0: "justify", 1: "left", 2: "right", 3: "center", 4: "justify", 5: "justify"}
_INLINE_EXT = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}   # 8 글자(16바이트)를 차지하는 제어 문자


class HwpError(Exception):
    pass


def _records(data: bytes):
    i, n = 0, len(data)
    while i + 4 <= n:
        h = struct.unpack_from("<I", data, i)[0]
        i += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, h >> 20
        if size == 0xFFF:
            size = struct.unpack_from("<I", data, i)[0]
            i += 4
        yield tag, level, data[i:i + size]
        i += size


def _tree(data: bytes) -> list:
    """레벨 값으로 레코드를 트리로 묶음: [tag, payload, children]."""
    root, stack = [], []           # stack: (level, node)
    for tag, level, payload in _records(data):
        node = [tag, payload, []]
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1][2] if stack else root).append(node)
        stack.append((level, node))
    return root


def _para_text(payload: bytes) -> tuple[str, list[int]]:
    """PARA_TEXT → (글, 글 위치→원래 글자 위치 표). 제어 문자는 줄바꿈·탭만 살림."""
    out, pos_map, i, wpos = [], [], 0, 0
    n = len(payload) // 2
    while i < n:
        ch = struct.unpack_from("<H", payload, i * 2)[0]
        if ch < 32:
            if ch in _INLINE_EXT:
                if ch == 9:
                    out.append(" "); pos_map.append(wpos)
                i += 8; wpos += 8
                continue
            if ch == 10:
                out.append("\n"); pos_map.append(wpos)
            elif ch in (24, 30, 31):
                out.append(" "); pos_map.append(wpos)
            i += 1; wpos += 1
            continue
        out.append(chr(ch)); pos_map.append(wpos)
        i += 1; wpos += 1
    return "".join(out), pos_map


def _doc_info(data: bytes) -> tuple[list, list]:
    chars, paras = [], []
    for tag, _lv, p in _records(data):
        if tag == T_CHAR_SHAPE and len(p) >= 50:
            size = struct.unpack_from("<i", p, 42)[0] / 100
            prop = struct.unpack_from("<I", p, 46)[0]
            chars.append({"size": size, "bold": bool(prop & 2)})
        elif tag == T_PARA_SHAPE and len(p) >= 4:
            paras.append({"align": ALIGN.get((struct.unpack_from("<I", p, 0)[0] >> 2) & 7, "justify")})
    return chars, paras


def _hwp_paragraph(node, chars, paras) -> list:
    """PARA_HEADER 노드 → 블록들 (문단 글 + 그 안의 표)."""
    p = node[1]
    shape_id = struct.unpack_from("<H", p, 8)[0] if len(p) >= 10 else 0
    align = paras[shape_id]["align"] if shape_id < len(paras) else "justify"
    text, pos_map, runs_shape, blocks, tables = "", [], [], [], []
    for child in node[2]:
        tag, payload, kids = child
        if tag == T_PARA_TEXT:
            text, pos_map = _para_text(payload)
        elif tag == T_PARA_CHAR_SHAPE:
            runs_shape = [struct.unpack_from("<II", payload, k) for k in range(0, len(payload) - 7, 8)]
        elif tag == T_CTRL_HEADER and payload[:4] == b" lbt":       # 'tbl ' (거꾸로 저장됨)
            tables.append(_hwp_table(child, chars, paras))
    # 글자 모양 구간 → (글, 굵게) 묶음
    runs, size = [], 0.0
    if text:
        shapes = runs_shape or [(0, 0)]
        def shape_at(w):
            sid = 0
            for start, s in shapes:
                if start <= w:
                    sid = s
            return chars[sid] if sid < len(chars) else {"size": 10, "bold": False}
        cur, cur_bold = "", None
        for ch, w in zip(text, pos_map):
            sh = shape_at(w)
            size = max(size, sh["size"])
            if cur_bold is not None and sh["bold"] != cur_bold:
                runs.append((cur, cur_bold)); cur = ""
            cur += ch; cur_bold = sh["bold"]
        if cur:
            runs.append((cur, bool(cur_bold)))
    if any(t.strip() for t, _ in runs) or not tables:
        blocks.append({"t": "p", "runs": runs, "align": align, "size": size or 10})
    blocks += tables
    return blocks


def _hwp_table(ctrl, chars, paras) -> dict:
    cells, cur = [], None
    for child in ctrl[2]:
        tag, payload, kids = child
        if tag == T_LIST_HEADER and len(payload) >= 16:
            col, row, cs, rs = struct.unpack_from("<HHHH", payload, 8)
            cur = {"row": row, "col": col, "colspan": max(cs, 1), "rowspan": max(rs, 1), "blocks": []}
            cells.append(cur)
            for k in kids:                                       # 셀 문단이 자식으로 들어오는 경우
                if k[0] == T_PARA_HEADER:
                    cur["blocks"] += _hwp_paragraph(k, chars, paras)
        elif tag == T_PARA_HEADER and cur is not None:          # 셀 문단이 LIST_HEADER 의 형제로 들어오는 경우
            cur["blocks"] += _hwp_paragraph(child, chars, paras)
    rows: dict[int, list] = {}
    for c in cells:
        rows.setdefault(c["row"], []).append(c)
    return {"t": "table", "rows": [sorted(rows[r], key=lambda c: c["col"]) for r in sorted(rows)]}


def read_hwp(path: str) -> list:
    import olefile
    try:
        ole = olefile.OleFileIO(path)
    except OSError as ex:
        raise HwpError("한글(.hwp) 파일이 아니거나 손상되었습니다") from ex
    with ole:
        hdr = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", hdr, 36)[0]
        if flags & 2:
            raise HwpError("암호가 걸린 한글 파일은 읽을 수 없습니다")
        unzip = (lambda b: zlib.decompress(b, -15)) if flags & 1 else (lambda b: b)
        chars, paras = _doc_info(unzip(ole.openstream("DocInfo").read()))
        blocks, k = [], 0
        while ole.exists(f"BodyText/Section{k}"):
            for node in _tree(unzip(ole.openstream(f"BodyText/Section{k}").read())):
                if node[0] == T_PARA_HEADER:
                    blocks += _hwp_paragraph(node, chars, paras)
            k += 1
    if not blocks:
        raise HwpError("본문을 찾지 못했습니다 (배포용·보안 문서는 읽을 수 없습니다)")
    return blocks


# ── HWPX (OWPML, XML) ─────────────────────────────────────────────────────
def _ln(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def read_hwpx(path: str) -> list:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as ex:
        raise HwpError("한글(.hwpx) 파일이 아니거나 손상되었습니다") from ex
    with z:
        names = z.namelist()
        header = ET.fromstring(z.read("Contents/header.xml")) if "Contents/header.xml" in names else None
        sections = sorted((n for n in names if re.fullmatch(r"Contents/section\d+\.xml", n)),
                          key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
        paras, chars = {}, {}
        if header is not None:
            for el in header.iter():
                if _ln(el) == "paraPr":
                    al = next((c.get("horizontal") for c in el.iter() if _ln(c) == "align"), "JUSTIFY")
                    paras[el.get("id")] = {"LEFT": "left", "RIGHT": "right", "CENTER": "center"}.get((al or "").upper(), "justify")
                elif _ln(el) == "charPr":
                    chars[el.get("id")] = {"size": int(el.get("height") or 1000) / 100,
                                           "bold": any(_ln(c) == "bold" for c in el)}
        blocks = []
        for s in sections:
            root = ET.fromstring(z.read(s))
            for p in root:
                if _ln(p) == "p":
                    blocks += _hwpx_paragraph(p, paras, chars)
    if not blocks:
        raise HwpError("본문을 찾지 못했습니다")
    return blocks


def _hwpx_paragraph(p, paras, chars) -> list:
    runs, tables, size = [], [], 0.0
    for run in p:
        if _ln(run) != "run":
            continue
        sh = chars.get(run.get("charPrIDRef"), {"size": 10, "bold": False})
        for el in run:
            name = _ln(el)
            if name == "t":
                txt = "".join(el.itertext())
                if txt:
                    runs.append((txt, sh["bold"])); size = max(size, sh["size"])
            elif name == "tbl":
                tables.append(_hwpx_table(el, paras, chars))
            elif name in ("lineBreak",):
                runs.append(("\n", False))
    blocks = []
    if any(t.strip() for t, _ in runs) or not tables:
        blocks.append({"t": "p", "runs": runs, "align": paras.get(p.get("paraPrIDRef"), "justify"), "size": size or 10})
    return blocks + tables


def _hwpx_table(tbl, paras, chars) -> dict:
    rows = []
    for tr in tbl:
        if _ln(tr) != "tr":
            continue
        row = []
        for tc in tr:
            if _ln(tc) != "tc":
                continue
            span = next((c for c in tc if _ln(c) == "cellSpan"), None)
            cell = {"colspan": int(span.get("colSpan", 1)) if span is not None else 1,
                    "rowspan": int(span.get("rowSpan", 1)) if span is not None else 1, "blocks": []}
            for sub in tc:
                if _ln(sub) == "subList":
                    for p in sub:
                        if _ln(p) == "p":
                            cell["blocks"] += _hwpx_paragraph(p, paras, chars)
            row.append(cell)
        rows.append(row)
    return {"t": "table", "rows": rows}


def read(path: str, filename: str) -> list:
    return read_hwpx(path) if filename.lower().endswith(".hwpx") else read_hwp(path)
