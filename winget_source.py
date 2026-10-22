"""调用 winget，解析“可更新列表”和“更新说明”。"""
import re
import subprocess

CREATE_NO_WINDOW = 0x08000000


def decode(raw):
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "utf-16-le"):
        try:
            txt = raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
        if enc == "utf-8" and "\ufffd" in txt:
            continue
        return txt
    return raw.decode("utf-8", "replace")


def run(args, timeout=240):
    try:
        p = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return 127, "", "找不到 winget"
    except subprocess.TimeoutExpired:
        return 124, "", "winget 执行超时"
    return p.returncode, decode(p.stdout), decode(p.stderr)


def _columns(sep_line):
    cols, start = [], None
    for i, ch in enumerate(sep_line):
        if ch == "-" and start is None:
            start = i
        elif ch != "-" and start is not None:
            cols.append((start, i))
            start = None
    if start is not None:
        cols.append((start, len(sep_line)))
    return cols


_SOURCES = ("winget", "msstore")


def parse_table(text):
    """解析 winget 表格。

    分隔行是一整条没有空格的虚线，无法用来切列；而长名称会把后面的列整体右推，
    按表头位置切也会错位。所以从右往左取：来源 / 最新 / 当前，剩下的是 名称 + Id。
    """
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s or set(s) <= set("- "):
            continue
        toks = s.split()
        if len(toks) < 5:
            continue
        source = toks[-1].lower()
        if source not in _SOURCES:
            continue
        latest, current = toks[-2], toks[-3]
        mid = toks[:-3]
        pid, name_toks = "", mid
        for k in range(len(mid) - 1, -1, -1):
            t = mid[k]
            if "." in t and re.search(r"[A-Za-z]", t) and not t.endswith("."):
                pid = t
                name_toks = mid[:k]
                break
        if not pid:
            continue
        rows.append({
            "name": " ".join(name_toks).strip() or pid,
            "id": pid,
            "current": current,
            "latest": latest,
            "source": source,
        })
    return rows


def upgrade_list(timeout=300):
    """返回 (可更新项列表, 原始输出, 退出码)。"""
    rc, out, err = run(
        ["winget", "upgrade", "--include-unknown",
         "--accept-source-agreements", "--disable-interactivity"],
        timeout=timeout,
    )
    items = parse_table(out)
    return items, out + err, rc


_FIELD = re.compile(r"^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5 /\u00b7]{0,24}[:：]")


def show_notes(pid, timeout=120):
    rc, out, err = run(
        ["winget", "show", "--id", pid, "-e",
         "--accept-source-agreements", "--disable-interactivity"],
        timeout=timeout,
    )
    text = out or err
    notes, url, ver = "", "", ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        low = s.lower()
        if not s:
            continue
        if low.startswith("release notes url") or "发布说明 url" in low or "发行说明 url" in low:
            m = re.match(r"^[^:：]*[:：]\s*(\S+)", s)
            if m:
                url = m.group(1)
            continue
        if low.startswith("version") or s.startswith("版本"):
            m = re.match(r"^[^:：]*[:：]\s*(.+)$", s)
            if m and not ver:
                ver = m.group(1).strip()
            continue
        if low.startswith("release notes") or s.startswith("发布说明") or s.startswith("发行说明"):
            buf = []
            m = re.match(r"^[^:：]*[:：]\s*(.*)$", s)
            if m and m.group(1).strip():
                buf.append(m.group(1).strip())
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt:
                    j += 1
                    continue
                if _FIELD.match(nxt):
                    break
                buf.append(nxt)
                j += 1
            if buf:
                notes = "\n".join(buf)
    return {"notes": notes, "url": url, "version": ver}
