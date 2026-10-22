"""版本号解析与比较（容错：v 前缀、未知、非数字后缀等）。"""
import re

_SPLIT = re.compile(r"[.\-_+~ ]+")


def parse(v):
    """把 'v1.2.3-beta' -> (1, 2, 3)；无法解析返回 ()。"""
    if v is None:
        return ()
    s = str(v).strip().lstrip("vV")
    if not s or not s[0].isdigit():
        return ()
    out = []
    for part in _SPLIT.split(s):
        m = re.match(r"^(\d+)", part)
        if not m:
            break
        out.append(int(m.group(1)))
    return tuple(out)


def pad(t, n=5):
    t = list(t[:n])
    t += [0] * (n - len(t))
    return tuple(t)


def is_newer(candidate, current):
    """candidate 是否比 current 新。当前版本无法解析时视为需要更新。"""
    a = parse(candidate)
    if not a:
        return False
    b = parse(current)
    if not b:
        return True
    return pad(a) > pad(b)


def same(a, b):
    pa, pb = parse(a), parse(b)
    return bool(pa) and bool(pb) and pad(pa) == pad(pb)
