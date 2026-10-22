"""升级执行：winget 覆盖升级 + 绿色版下载覆盖（带备份、下载速度与进度回调）。

progress 回调收到的 dict：
    phase   : download / extract / apply / install / query
    app     : 程序名
    done    : 已处理字节数（download）
    total   : 总字节数（download，未知时为 0）
    percent : 0~100，未知时为 None
    speed   : 字节/秒（download，滑动窗口平均）
    eta     : 剩余秒数或 None
"""
import ctypes
import hashlib
import io
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import gh
import version
from winget_source import CREATE_NO_WINDOW, decode

BASE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE, "backup")
CACHE_DIR = os.path.join(BASE, "cache")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 OneKeyUpdater/0.4")
# Accept 一定要带：Epic 的 CDN 见到"没有 Accept"的请求会当成机器人，
# 302 到 /auth/login?returnPath=<原地址>，登录页再跳回下载地址 → 死循环。
UA_HDR = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
CHUNK = 256 * 1024
# 多连接下载：文件 >= 该阈值才分段（避免小文件开太多连接反而慢）
MIN_SPLIT = 2 * 1024 * 1024
MAX_CONN = 8

# GitHub 加速镜像（国内直连 github.com 经常 10060 超时）。按实测可用性排序。
GH_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")
_MAX_REDIRECT = 8

_SIZE_RE = re.compile(r"([\d.]+)\s*(B|KB|MB|GB)\s*/\s*([\d.]+)\s*(B|KB|MB|GB)", re.I)
_PCT_RE = re.compile(r"([\d.]+)\s*%")
_UNIT = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", (name or "app")).strip() or "app"


def _emit(progress, **kw):
    if not progress:
        return
    try:
        progress(kw)
    except Exception:
        pass


def _result(ok, status, reason=""):
    """统一的升级结果：ok=是否成功；status=分类(success/not_applicable/
    permission/failed/cancelled)；reason=中文原因，用于在界面上标注。"""
    return {"ok": bool(ok), "status": status, "reason": reason or ""}


def _classify_oserr(e):
    """把 OSError 归类为 权限/占用 或 其他系统错误。"""
    msg = str(e)
    winerr = getattr(e, "winerror", None)
    if ("无法访问" in msg or "拒绝访问" in msg or "access is denied" in msg.lower()
            or winerr in (5, 32, 1920)):
        return _result(False, "permission",
                       "系统无法访问文件：需管理员权限，或先关闭该程序")
    return _result(False, "failed", "系统错误：%s" % msg)


class _Cancel(Exception):
    """下载/升级被用户取消。"""
    pass


class SpeedMeter:
    """滑动窗口测速，避免瞬时抖动让数字乱跳。"""

    def __init__(self, window=3.0):
        self.window = window
        self.samples = [(time.monotonic(), 0)]

    def reset(self):
        self.samples = [(time.monotonic(), 0)]

    def update(self, done):
        now = time.monotonic()
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.window:
            self.samples.pop(0)
        t0, b0 = self.samples[0]
        dt = now - t0
        if dt <= 0.001:
            return 0.0
        return max(0.0, (done - b0) / dt)


# ---------------------------------------------------------------- 下载

def _safe_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _elapsed(meter):
    if not meter.samples:
        return 0.001
    return max(0.001, time.monotonic() - meter.samples[0][0])


class _RedirectLoop(Exception):
    """跳转目标回到原地址（典型：下载链接要求登录，登录页又跳回下载地址）。"""

    def __init__(self, url, target=""):
        Exception.__init__(self, "redirect loop: %s" % target[:120])
        self.url = url
        self.target = target


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """关闭自动跟随，交给 _resolve 手动处理——这样才能识别"跳回自己"的循环。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 通道状态：国内有两种相反的故障——
#   ① 系统代理开着但隧道坏了（502 / RemoteDisconnected）
#   ② 没走代理时 GitHub 被墙（10060 超时）
# 所以两个通道都要试，并记住上次能用的那个，后续优先用它。
_NET = {"mode": None}


def _net_mark(direct):
    _NET["mode"] = "direct" if direct else "proxy"


def _open_once(url, headers, timeout, direct, follow):
    handlers = []
    if not follow:
        handlers.append(_NoRedirect())
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=headers)
    return opener.open(req, timeout=timeout)


def _is_net_error(e):
    """是否属于"换个通道/重试可能好"的网络问题（HTTP 状态码错误不算）。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (403, 407, 408, 425, 429, 500, 502, 503, 504)
    return isinstance(e, (urllib.error.URLError, OSError, TimeoutError))


def _net_hint(e):
    """把底层网络异常翻译成能看懂的中文原因。"""
    s = (str(e) + " " + repr(e)).lower()
    if "tunnel connection failed" in s or " 502" in s:
        return "代理不可用（502），已尝试直连仍失败"
    if "10060" in s or "timed out" in s or "timeout" in s:
        return "连接超时（网络不通或被拦截）"
    if "remotedisconnected" in s or "connection reset" in s or "aborted" in s \
            or "connection closed" in s:
        return "连接被中途断开（网络不稳）"
    if "certificate" in s or "ssl" in s:
        return "HTTPS 证书/握手失败"
    if "getaddrinfo" in s or "name or service not known" in s:
        return "域名解析失败（DNS 异常）"
    return ""


def _open(url, headers=None, timeout=60, follow=True, tries=3):
    """打开 URL：系统代理 ↔ 直连 双通道 + 重试。返回 response。

    HTTP 状态码错误（4xx/5xx）不重试，直接抛给上层判断——重试也没用。
    """
    hdr = dict(UA_HDR)
    if headers:
        hdr.update(headers)
    order = [False, True] if _NET["mode"] != "direct" else [True, False]
    last = None
    for attempt in range(max(1, tries)):
        for direct in order:
            try:
                resp = _open_once(url, hdr, timeout, direct, follow)
                _net_mark(direct)
                return resp
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    raise            # 交给 _resolve 手动跟随
                if not _is_net_error(e):
                    raise
                last = e
            except Exception as e:
                if isinstance(e, _Cancel) or not _is_net_error(e):
                    raise
                last = e
        if attempt < tries - 1:
            time.sleep(0.8 * (attempt + 1))
    raise last if last else IOError("网络请求失败")


def _resolve(url, headers=None, timeout=15, log=None):
    """手动跟随跳转，返回 (最终地址, 最后一段的响应头)。

    为什么不用 urllib 自动跟随：跳转回到自身时它抛的是
    "redirect error that would lead to an infinite loop"，完全看不出
    真实原因（多半是"这个链接要登录"）。这里自己走，能给出准确提示。
    """
    cur = url
    hops = 0
    while hops < _MAX_REDIRECT:
        try:
            resp = _open(cur, headers, timeout=timeout, follow=False, tries=2)
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            loc = e.headers.get("Location") or ""
            e.close()
            if not loc:
                raise IOError("HTTP %d 没有 Location" % e.code)
            nxt = urllib.parse.urljoin(cur, loc)
            if nxt == cur or "auth/login" in nxt or "signin" in nxt.lower():
                raise _RedirectLoop(cur, nxt)
            cur = nxt
            hops += 1
            continue
        hdrs = resp.headers
        resp.close()
        return cur, hdrs
    raise IOError("跳转层数过多（>%d）" % _MAX_REDIRECT)


def _probe_range(url, log=None, timeout=15):
    """探测：返回 (最终下载地址, 总字节数, 是否支持 Range 分段)。

    超时给得短（15s）：探测只是为了判断"能不能下"，网络不通就该尽快
    换下一个通道/镜像，而不是让整个下载卡几分钟。
    """
    try:
        final, _h = _resolve(url, timeout=timeout)
    except _RedirectLoop as e:
        if log:
            log("  该下载地址要求登录/会话（跳转到了登录页），直链下载不可用")
            log("    跳转目标: %s" % e.target[:110])
        return url, 0, False
    except Exception as e:
        if log:
            hint = _net_hint(e)
            if hint:
                log("  连接探测失败：%s" % hint)
        return url, 0, False
    try:
        with _open(final, {"Range": "bytes=0-0"}, timeout=timeout, tries=2) as r:
            cr = r.headers.get("Content-Range", "")
            ar = (r.headers.get("Accept-Ranges") or "").lower()
            if cr.startswith("bytes") and "/" in cr:
                total = int(cr.rsplit("/", 1)[1])
            elif r.headers.get("Content-Length"):
                total = int(r.headers["Content-Length"])
            else:
                total = 0
            can = (ar == "bytes") or cr.startswith("bytes")
            return final, total, (can and total > 0)
    except Exception:
        return final, 0, False


def _mirror_urls(url):
    """GitHub 直连常超时/被断，给出备用加速通道（原地址放最后再兜一次）。"""
    low = (url or "").lower()
    if "github.com/" not in low and "githubusercontent.com/" not in low:
        return []
    return [m + url for m in GH_MIRRORS] + [url]


def download(url, dest, log=print, progress=None, app_name="", cancel=None,
             expect_sha256=None):
    """下载到 dest；大文件自动多连接分段（拉满带宽），小文件走单流。

    全程回报「每秒速度 + 进度」。cancel() 返回 True 时中止并清理半截文件。
    返回 dest 路径表示成功；None 表示失败/取消。

    失败会自动换通道重试：先本地址（系统代理/直连两个通道都试），
    GitHub 的还会依次试加速镜像——单次连接失败不再等于整个下载失败。

    完整性是硬要求：连接中途断开时 resp.read() 只会返回空串、不会抛异常，
    以前会把半截文件当成下载完成改名存下，安装器随后报"完整性校验失败"
    一闪而过（NSIS 退出码 2）——这就是"装不动"的一个真实来源。
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    candidates = [url] + _mirror_urls(url)
    last_hint = ""
    for n, u in enumerate(candidates, 1):
        if cancel and cancel():
            log("  下载已取消")
            return None
        if n > 1:
            host = urllib.parse.urlsplit(u).netloc or u
            log("  换通道重试（%d/%d）: %s" % (n, len(candidates), host))
            _emit(progress, phase="download", app=app_name, done=0, total=0,
                  percent=None, speed=0, eta=None)
        final, length, can_range = _probe_range(u, log=log if n == 1 else None)
        if can_range and length >= MIN_SPLIT:
            got = _download_ranged(final, length, dest, log, progress, app_name, cancel)
        else:
            got = _download_single(final if final else u, dest, log, progress,
                                   app_name, cancel)
        if cancel and cancel():
            log("  下载已取消")
            return None
        if not got:
            last_hint = last_hint or _last_dl_error.get("hint", "")
            continue
        size = os.path.getsize(dest)
        if length and size != length:
            _safe_remove(dest)
            log("  文件不完整：应为 %d 字节，实得 %d 字节，已删除" % (length, size))
            continue
        if expect_sha256:
            want = expect_sha256.strip().lower()
            got_sha = _sha256(dest)
            if got_sha != want:
                log("  注意：SHA256 与清单不一致")
                log("    清单 %s… / 实际 %s…" % (want[:16], got_sha[:16]))
                log("    若安装失败，多半是这个包不完整或版本与清单不符，删掉缓存重新下载即可。")
            else:
                log("  SHA256 校验通过")
        return dest
    if last_hint:
        log("  下载最终失败：%s（已试过 %d 个通道）" % (last_hint, len(candidates)))
    return None


def _sha256(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


# 记录最近一次下载失败的通俗原因，供 download() 汇总提示
_last_dl_error = {"hint": ""}


def _download_single(url, dest, log, progress, app_name, cancel):
    part = dest + ".part"
    meter = SpeedMeter()
    last = 0.0
    _last_dl_error["hint"] = ""
    try:
        with _open(url, None, timeout=60, tries=2) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            _emit(progress, phase="download", app=app_name, done=0, total=total,
                  percent=0.0 if total else None, speed=0.0, eta=None)
            while True:
                if cancel and cancel():
                    raise _Cancel()
                b = resp.read(CHUNK)
                if not b:
                    break
                f.write(b)
                done += len(b)
                now = time.monotonic()
                if now - last >= 0.12:
                    last = now
                    sp = meter.update(done)
                    pct = (done * 100.0 / total) if total else None
                    eta = ((total - done) / sp) if (total and sp > 2048) else None
                    _emit(progress, phase="download", app=app_name, done=done,
                          total=total, percent=pct, speed=sp, eta=eta)
        # 连接中途断开时 read() 只返回空串、不抛异常，必须自己对账
        if total and done != total:
            raise IOError("连接中断：只收到 %d / %d 字节" % (done, total))
        os.replace(part, dest)
    except _Cancel:
        _safe_remove(part)
        log("  下载已取消")
        return None
    except Exception as e:
        _safe_remove(part)
        hint = _net_hint(e)
        _last_dl_error["hint"] = hint or str(e)[:120]
        if hint:
            log("  下载失败：%s" % hint)
            log("    （原始错误 %r）" % (e,))
        else:
            log("  下载失败: %r" % e)
        return None
    sp = meter.update(done)
    _emit(progress, phase="download", app=app_name, done=done,
          total=total or done, percent=100.0, speed=sp, eta=0)
    log("  下载完成: %s（约 %.2f MB/s）" % (_mb(done),
        (done / 1048576.0) / max(0.001, _elapsed(meter))))
    return dest


def _download_ranged(url, length, dest, log, progress, app_name, cancel):
    n = min(MAX_CONN, max(2, length // (1 * 1024 * 1024)))
    chunk = length // n
    segments = []
    for i in range(n):
        s = i * chunk
        e = length - 1 if i == n - 1 else s + chunk - 1
        segments.append((s, e))
    part_files = [dest + ".part%d" % i for i in range(n)]
    done = [0] * n
    lock = threading.Lock()
    meter = SpeedMeter()
    last = [0.0]
    finished = threading.Event()
    err = [None]

    def worker(i, s, e):
        try:
            with _open(url, {"Range": "bytes=%d-%d" % (s, e)}, timeout=60, tries=2) as r, \
                    open(part_files[i], "wb") as f:
                while True:
                    if cancel and cancel():
                        raise _Cancel()
                    b = r.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    done[i] += len(b)
        except _Cancel:
            raise
        except Exception as ex:
            err[0] = ex

    def monitor():
        while not finished.is_set():
            if cancel and cancel():
                break
            now = time.monotonic()
            td = sum(done)
            sp = meter.update(td)
            pct = td * 100.0 / length if length else 0
            eta = (length - td) / sp if sp > 2048 else None
            if now - last[0] >= 0.12:
                last[0] = now
                _emit(progress, phase="download", app=app_name, done=td,
                      total=length, percent=pct, speed=sp, eta=eta)
            time.sleep(0.05)

    threads = [threading.Thread(target=worker, args=(i, s, e))
               for i, (s, e) in enumerate(segments)]
    mon = threading.Thread(target=monitor)
    for t in threads:
        t.start()
    mon.start()
    for t in threads:
        t.join()
    finished.set()
    mon.join()

    if cancel and cancel():
        for p in part_files:
            _safe_remove(p)
        log("  下载已取消")
        return None
    if err[0] is not None:
        for p in part_files:
            _safe_remove(p)
        hint = _net_hint(err[0])
        _last_dl_error["hint"] = hint or str(err[0])[:120]
        if hint:
            log("  下载失败（分段）：%s" % hint)
            log("    （原始错误 %r）" % (err[0],))
        else:
            log("  下载失败（分段）: %r" % (err[0],))
        return None
    # 每段都必须足量，少一个字节都说明连接被掐断过
    for i, (s, e) in enumerate(segments):
        want_len = e - s + 1
        try:
            got_len = os.path.getsize(part_files[i])
        except OSError:
            got_len = -1
        if got_len != want_len:
            for p in part_files:
                _safe_remove(p)
            log("  分段 %d 不完整：应为 %d 字节，实得 %d 字节，已放弃"
                % (i, want_len, got_len))
            return None

    try:
        with open(dest, "wb") as out:
            for p in part_files:
                with open(p, "rb") as f:
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            break
                        out.write(b)
                _safe_remove(p)
    except Exception as e:
        for p in part_files:
            _safe_remove(p)
        _safe_remove(dest)
        log("  合并失败: %r" % e)
        return None
    sp = meter.update(sum(done))
    _emit(progress, phase="download", app=app_name, done=length, total=length,
          percent=100.0, speed=sp, eta=0)
    log("  下载完成（多连接 %d 路）: %s 约 %.2f MB/s"
        % (n, _mb(length), (length / 1048576.0) / max(0.001, _elapsed(meter))))
    return dest


def _mb(n):
    return "%.2f MB" % (n / 1048576.0)


# ---------------------------------------------------------------- winget

def _parse_winget_progress(line):
    """从 winget 的进度行里抠出 已下载/总量 或百分比。"""
    m = _SIZE_RE.search(line)
    if m:
        try:
            done = float(m.group(1)) * _UNIT[m.group(2).upper()]
            total = float(m.group(3)) * _UNIT[m.group(4).upper()]
        except (KeyError, ValueError):
            return None
        return {"done": int(done), "total": int(total),
                "percent": (done * 100.0 / total) if total else None}
    m = _PCT_RE.search(line)
    if m:
        try:
            return {"done": 0, "total": 0, "percent": float(m.group(1))}
        except ValueError:
            return None
    return None


def update_winget(app, log=print, timeout=1800, progress=None, cancel=None):
    pid = app["id"]
    name = app.get("name") or pid
    args = ["winget", "upgrade", "--id", pid, "-e", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity"]
    log("执行: " + " ".join(args))
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        log("找不到 winget，无法升级 %s" % name)
        return _result(False, "failed", "系统中没有 winget")
    except OSError as e:
        log("启动 winget 失败: %r" % e)
        return _classify_oserr(e)

    _emit(progress, phase="install", app=name, percent=None)
    start = time.time()
    buf = b""
    last_emit = 0.0
    not_applicable = False
    access_err = False
    try:
        while True:
            if cancel and cancel():
                p.kill()
                log("  已取消")
                return _result(False, "cancelled", "用户取消")
            chunk = p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = decode(raw).strip()
                if not line:
                    continue
                info = _parse_winget_progress(line)
                now = time.monotonic()
                if info:
                    if now - last_emit >= 0.15:
                        last_emit = now
                        _emit(progress, phase="install", app=name,
                              done=info["done"], total=info["total"],
                              percent=info["percent"], speed=None, eta=None)
                    continue
                low = line.lower()
                if ("找不到适用的升级" in line or "不适用于你的系统" in line
                        or "no applicable" in low):
                    not_applicable = True
                if ("无法访问此文件" in line or "拒绝访问" in line
                        or "access is denied" in low):
                    access_err = True
                log("  " + line)
            if time.time() - start > timeout:
                p.kill()
                log("  超时，已终止")
                return _result(False, "failed", "升级超时（%d 秒）" % timeout)
    except OSError as e:
        log("读取 winget 输出失败: %r" % e)
        return _classify_oserr(e)

    tail = decode(buf).strip()
    if tail:
        log("  " + tail)
    rc = p.wait()
    log("  退出码: %d" % rc)
    if rc == 0:
        _emit(progress, phase="install", app=name, percent=100.0)
        return _result(True, "success", "")
    if not_applicable:
        return _result(False, "not_applicable",
                       "源有新版，但不适用于本机系统/架构")
    if access_err:
        return _result(False, "permission",
                       "系统无法访问文件：需管理员权限，或先关闭该程序")
    return _result(False, "failed",
                   "退出码 %d：可能需管理员权限或该版本无法静默升级" % rc)


# ---------------------------------------------------------------- 两步式：先下载，后安装

def _run_capture(args, timeout=120):
    """运行命令并返回 (rc, out_text, err_text)，解码尽量鲁棒（utf-8→gbk→latin-1）。"""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except Exception as e:
        return -1, "", str(e)

    def dec(b):
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return b.decode(enc)
            except Exception:
                pass
        return b.decode("utf-8", "replace")

    return p.returncode, dec(p.stdout), dec(p.stderr)


_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff\"'<>]+", re.I)
_BIN_EXT = (".exe", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle", ".zip")


def _score_url(u):
    """给候选 URL 打分：安装包特征越多分越高。"""
    low = u.lower()
    s = 0
    if low.endswith(_BIN_EXT):
        s += 3
    if any(k in low for k in ("x64", "amd64", "x86_64")):
        s += 2
    if any(k in low for k in ("setup", "install", "download", "release")):
        s += 1
    return s


def _pick_installer_url(out):
    """从 winget show 输出里用正则抠出最可能的安装包下载地址。"""
    urls = []
    for u in _URL_RE.findall(out):
        u = u.rstrip("。.,;、）)】]\"'>")
        if u:
            urls.append(u)
    if not urls:
        return None, ""
    # 安装包类型（exe/msi/wix/inno/nsis…），中英文标签、全/半角冒号都兼容
    itype = ""
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("安装程序类型") or s.lower().startswith("installer type"):
            part = s.split("：", 1)[-1].split(":", 1)[-1]
            itype = part.strip().lower()
            break
    # 按打分排序，挑最像安装包的那个（同分优先更靠后的，往往是真正的 installer）
    ranked = sorted(set(urls), key=_score_url, reverse=True)
    # 先挑带二进制后缀的；都没有再退而求其次取最高分
    for u in ranked:
        if u.lower().endswith(_BIN_EXT):
            return u, itype
    return ranked[0], itype


def _winget_download_fallback(pid, log=print):
    """URL 解析失败时的兜底：让 winget 自己把安装包下载到本地（黑盒，无精确进度）。

    返回下载到的文件路径，失败返回 None。
    """
    d = os.path.join(CACHE_DIR, "_wd_" + _safe(pid))
    os.makedirs(d, exist_ok=True)
    args = ["winget", "download", "--id", pid, "-e", "-d", d,
            "--accept-source-agreements", "--disable-interactivity"]
    rc, out, err = _run_capture(args, 600)
    if rc != 0:
        log("  winget download 失败：%s" % ((err or out).strip()[:240]))
        return None
    files = []
    for root, _dirs, fnames in os.walk(d):
        for f in fnames:
            if f.lower().endswith(_BIN_EXT):
                files.append(os.path.join(root, f))
    if files:
        return files[0]
    return None


def _pick_installer_sha(text):
    """从 winget show 输出里取安装包 SHA256（用于下载后完整性校验）。"""
    m = re.search(r"SHA256[：:]\s*([0-9a-fA-F]{64})", text or "")
    return m.group(1).lower() if m else None


def _ext_from_itype(itype):
    """从 winget 的「安装程序类型」推扩展名（如 `exe (zip)` → `.zip`）。"""
    s = (itype or "").lower()
    if "zip" in s:
        return ".zip"
    if "msix" in s or "appx" in s:
        return ".msix"
    if "msi" in s:
        return ".msi"
    if s.startswith("exe") or "inno" in s or "nsis" in s or "wix" in s:
        return ".exe"
    return ""


def _winget_fname(app, url, itype):
    """给下载物取个"带扩展名"的名字。

    真实坑：Epic Online Services 的清单地址是
      https://onlineservices.epicgames.com/api/cosmos/sdk/download?archive_id=870
    路径最后一段叫 `download`，没有任何后缀 → 存下来的文件既认不出类型、
    也没法双击，安装阶段直接报 WinError 193。所以名字里没后缀时，按 winget
    声明的安装器类型补，再不行用「包名-版本」+ `.dl`，下载完按内容再纠正。
    """
    base = os.path.basename((url or "").split("?")[0] or "")
    stem, ext = os.path.splitext(base)
    if ext.lower() in _PKG_EXT:
        return _safe(base)
    ext = _ext_from_itype(itype) or ".dl"
    tag = _safe("%s-%s" % ((app or {}).get("id") or "",
                           (app or {}).get("latest") or "")).strip("-_")
    return (tag or _safe(stem) or "installer") + ext


def get_winget_installer(app, log=print):
    """用 `winget show` 取出安装包的真实下载地址与类型。

    返回 {ok, url, itype, name} 或 {ok:False, reason, need_direct}。
    need_direct=True 表示地址解析失败，但可退回到 winget download 直接下载。
    """
    pid = app["id"]
    args = ["winget", "show", "--id", pid, "-e",
            "--accept-source-agreements", "--disable-interactivity"]
    for attempt in (1, 2):
        rc, out, err = _run_capture(args, 120)
        if rc != 0 and attempt == 1:
            log("  winget show 第 1 次返回码 %d，重试一次…" % rc)
            time.sleep(1.5)
            continue
        url, itype = _pick_installer_url(out)
        if url:
            fname = _winget_fname(app, url, itype)
            return {"ok": True, "url": url, "itype": itype or "", "name": fname,
                    "sha256": _pick_installer_sha(out)}
        if rc != 0:
            log("  winget show 返回码 %d：%s" % (rc, (err or out).strip()[:200]))
            break
        # 取到了输出但没解析出 URL：再给一次机会（偶发源未同步）
        if attempt == 1:
            log("  未从输出中解析出安装包地址，重试一次…")
            time.sleep(1.5)
    return {"ok": False,
            "reason": "无法从 winget 解析出安装包地址（已确认官网有此版本，可能是联网/源同步问题）",
            "need_direct": True}


def _adopt_legacy_cache(url, dest, log=print):
    """复用旧版本留下的"没扩展名的下载物"。

    旧逻辑把 Epic 那种无文件名地址存成了 `cache\\download`（587MB）。改好命名后
    路径变了，如果不接管这份缓存，用户得再下一次 587MB。这里只认"URL 末段同名、
    类型对得上"的文件，确认后才改名搬家。
    """
    if os.path.exists(dest):
        return dest
    base = os.path.basename((url or "").split("?")[0] or "")
    if not base:
        return dest
    legacy = os.path.join(CACHE_DIR, _safe(base))
    if legacy == dest or not os.path.isfile(legacy):
        return dest
    want = os.path.splitext(dest)[1].lower()
    kind = sniff_kind(legacy)
    if want in _PKG_EXT and kind and _KIND_EXTS.get(kind, set()) and \
            want not in _KIND_EXTS[kind]:
        return dest
    try:
        os.replace(legacy, dest)
    except OSError as e:
        log("  复用旧缓存失败（忽略）：%r" % e)
        return dest
    log("  复用上次已下载的文件：%s → %s" % (os.path.basename(legacy),
                                            os.path.basename(dest)))
    return dest


def _fix_ext_by_sniff(dest, log=print):
    """下载完按文件头补扩展名——名字错了，后面每一步识别都会跟着错。"""
    ext = os.path.splitext(dest)[1].lower()
    if ext in _PKG_EXT:
        return dest
    kind = sniff_kind(dest)
    real = _KIND_EXT.get(kind, "")
    if not real:
        return dest
    new = dest + real
    try:
        if os.path.exists(new):
            os.remove(new)
        os.replace(dest, new)
    except OSError as e:
        log("  补扩展名失败（忽略）：%r" % e)
        return dest
    log("  按文件内容补上扩展名: %s → %s" % (os.path.basename(dest),
                                            os.path.basename(new)))
    return new


def download_app(app, log=print, progress=None, cancel=None):
    """第一步：把安装包下载到本地缓存（绿色版 / winget 统一走此函数）。

    返回 {ok, path, itype, url, name} 或 {ok:False, status, reason}。
    下载过程通过 progress 回调实时回报「每秒速度 + 进度」。
    """
    name = app.get("name") or app.get("id")
    if app.get("kind") == "winget":
        meta = get_winget_installer(app, log=log)
        if not meta.get("ok"):
            if meta.get("need_direct"):
                # 解析不到地址：退回 winget download 直接把安装包拉到本地（黑盒，无精确速度）
                log("改用 winget 直接下载安装包（无法显示精确速度）…")
                _emit(progress, phase="download", app=name, done=0, total=0,
                      percent=None, speed=0, eta=None)
                dpath = _winget_download_fallback(app["id"], log=log)
                if not dpath:
                    return {"ok": False, "status": "failed",
                            "reason": meta.get("reason", "下载失败")}
                dpath = _fix_ext_by_sniff(dpath, log)
                inner = _prepare_nested(dpath, app, log)
                if inner:
                    return {"ok": True, "path": inner, "itype": "", "url": "",
                            "name": name, "nested_from": dpath}
                return {"ok": True, "path": dpath, "itype": "",
                        "url": "", "name": os.path.basename(dpath)}
            return {"ok": False, "status": "failed",
                    "reason": meta.get("reason", "无法获取安装包")}
        url = meta["url"]
        itype = meta.get("itype") or ""
        fname = meta["name"]
        expect_sha = meta.get("sha256")
    else:  # 绿色版（有源）
        repo = (app.get("id") or "").strip()
        if not repo:
            return {"ok": False, "status": "failed", "reason": "未配置更新源（github），跳过"}
        try:
            rel = gh.latest_release(repo)
        except Exception as e:
            return {"ok": False, "status": "failed", "reason": "查询最新版本失败：%s" % e}
        asset = gh.pick_asset(rel, app.get("asset") or "*")
        if not asset:
            return {"ok": False, "status": "failed", "reason": "该发布没有可下载文件"}
        url = asset["browser_download_url"]
        itype = "zip" if (asset.get("name") or "").lower().endswith(".zip") else "file"
        fname = _safe(asset.get("name") or "download.bin")
        expect_sha = None

    dest = os.path.join(CACHE_DIR, fname)
    dest = _adopt_legacy_cache(url, dest, log)

    def _finish(p):
        inner = _prepare_nested(p, app, log)
        if inner:
            return {"ok": True, "path": inner, "itype": "", "url": url,
                    "name": name, "sha256": expect_sha, "nested_from": p}
        return {"ok": True, "path": p, "itype": itype, "url": url, "name": name,
                "sha256": expect_sha}

    # 缓存里已经有同哈希的包就不再重下（Epic 这种一次 560MB，白下一遍很伤）
    if expect_sha and os.path.isfile(dest):
        try:
            if _sha256(dest).lower() == expect_sha.strip().lower():
                log("缓存里已有完整安装包（SHA256 与官方清单一致），跳过下载")
                _emit(progress, phase="download", app=name, done=1, total=1,
                      percent=100.0, speed=0, eta=0)
                return _finish(dest)
        except OSError:
            pass

    log("下载 %s <- %s" % (name, url))
    dl = download(url, dest, log=log, progress=progress, app_name=name,
                  cancel=cancel, expect_sha256=expect_sha)
    if not dl:
        if cancel and cancel():
            return {"ok": False, "status": "cancelled", "reason": "用户取消"}
        hint = _last_dl_error.get("hint") or "网络不通或源地址失效"
        # 最后兜底：让 winget 自己下载。它的请求头和重试逻辑与 urllib 不同，
        # 直链被拦（如 Epic 的 CDN 要求登录/会话）时往往能成。
        if app.get("kind") == "winget" and app.get("id"):
            log("直链下载失败（%s），改让 winget 自己下载安装包…" % hint)
            _emit(progress, phase="download", app=name, done=0, total=0,
                  percent=None, speed=0, eta=None)
            dpath = _winget_download_fallback(app["id"], log=log)
            if dpath and os.path.exists(dpath):
                dpath = _fix_ext_by_sniff(dpath, log)
                inner = _prepare_nested(dpath, app, log)
                if inner:
                    return {"ok": True, "path": inner, "itype": "",
                            "url": url, "name": name, "sha256": expect_sha,
                            "nested_from": dpath}
                return {"ok": True, "path": dpath, "itype": itype or "",
                        "url": url, "name": name, "sha256": expect_sha}
        return {"ok": False, "status": "failed", "reason": "下载失败：%s" % hint}
    dest = _fix_ext_by_sniff(dest, log)
    return _finish(dest)


def _run(cmd, log, timeout=1800):
    """运行子进程，转发输出，返回退出码。"""
    log("执行: " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        log("启动失败: %r" % e)
        raise
    try:
        while True:
            chunk = p.stdout.read(4096)
            if not chunk:
                break
            for raw in re.split(rb"[\r\n]", chunk):
                line = decode(raw).strip()
                if line:
                    log("  " + line)
    finally:
        rc = p.wait()
    log("  退出码: %d" % rc)
    return rc


def install_app(app, pkg, log=print, progress=None, cancel=None):
    """第二步：用第一步下载到缓存里的安装包执行覆盖式安装。

    不会自动下载，必须用户点击「安装更新」后才会调用。
    安装过程会抓取安装器真实输出，失败时给出中文原因。
    """
    path = pkg.get("path")
    if is_msix(path):
        # MSIX 包（Windows 终端 / WSL / python-manager 这类）：必须走系统接口，
        # 双击安装包只会弹 App Installer，同样被"应用正在运行"挡住
        return install_msix(path, app, log=log, progress=progress, cancel=cancel)
    if is_zip(path):
        # ZIP 绿色版包（winget 的 zip 安装器，如 ripgrep）：只能解压覆盖，不能执行
        return install_zip_app(path, app, log=log, progress=progress, cancel=cancel)
    if app.get("kind") == "winget":
        return _install_winget(path, pkg.get("itype") or "", app,
                               log=log, progress=progress, cancel=cancel, pkg=pkg)
    # 绿色版：解压覆盖
    target = app.get("target") or (os.path.dirname(app["exe"]) if app.get("exe") else "")
    if not target or not os.path.isdir(target):
        return _result(False, "failed", "目标目录无效：%s" % target)
    try:
        applied = _apply(path, target, app, log=log, progress=progress)
    except OSError as e:
        return _classify_oserr(e)
    except Exception as e:
        return _result(False, "failed", "覆盖出错：%s" % e)
    if applied:
        return _result(True, "success", "")
    return _result(False, "failed", "覆盖未完成（文件可能被占用，建议先关闭该程序）")


# ---------------- 安装命令与结果分类 ----------------

# ---------------- 安装器识别 ----------------
#
# 安装包到底认哪个静默开关，看它用哪套安装器框架就知道——这是业界固定约定，
# 不需要"逐个参数试"。反过来，猜错时安装器只是把未知参数忽略掉，然后照常弹
# 出界面等人点，表现就是"弹窗 / 安装不动"。
#
# 特征串是从真实安装包里对出来的（QQ=自研封装、微信输入法=NSIS、
# 百度网盘=NSIS、Git=Inno Setup）。

_FAMILY_SIGS = (
    (b"Inno Setup Setup Data", "inno"),
    (b"NullsoftInst", "nsis"),
    (b"Nullsoft.NSIS", "nsis"),
    (b"NSIS Error", "nsis"),
    (b"InstallShield", "installshield"),
    (b"WixBundle", "wix"),
    (b"BurnStub", "wix"),
)

_FAMILY_LABEL = {
    "inno": "Inno Setup",
    "nsis": "NSIS",
    "installshield": "InstallShield",
    "wix": "WiX Burn",
    "msi": "Windows Installer (MSI)",
    "msix": "MSIX/APPX",
    "zip": "绿色版压缩包 (ZIP)",
    "unknown": "厂商自研封装",
}

_FAMILY_ARGS = {
    "inno": ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"],
    "nsis": ["/S"],
    "installshield": ["/s", "/v/qn"],
    "wix": ["/quiet", "/norestart"],
}

# winget 清单里的 InstallerType -> 安装器框架
_ITYPE_MAP = {
    "nullsoft": "nsis", "nsis": "nsis", "inno": "inno",
    "wix": "wix", "msi": "msi", "msix": "msix", "appx": "msix",
}


def detect_installer(path):
    """扫描安装包二进制判定安装器框架（只读特征串，不执行安装包）。

    返回 {"family", "label", "args", "confident"}。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in _PKG_EXT:
        # 扩展名缺失/不认识 → 按文件头重新认（Epic 会被存成 cache\download）
        kind = sniff_kind(path)
        if kind == "zip":
            ext = ".zip"
        elif kind == "msi":
            ext = ".msi"
        elif kind == "msix":
            ext = ".msix"
    if ext == ".msi":
        return {"family": "msi", "label": _FAMILY_LABEL["msi"],
                "args": [], "confident": True}
    if ext in (".msix", ".appx"):
        return {"family": "msix", "label": _FAMILY_LABEL["msix"],
                "args": [], "confident": True}
    # ZIP 不是安装程序，而是"解压即用"的绿色版包（winget 的 zip 安装器就是这样）。
    # 以前会把它当 exe 执行 → WinError 193「不是有效的 Win32 应用程序」。
    if ext == ".zip":
        return {"family": "zip", "label": _FAMILY_LABEL["zip"],
                "args": [], "confident": True}
    family = None
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                chunk = f.read(4 * 1024 * 1024)
                if not chunk:
                    break
                buf = tail + chunk
                for sig, fam in _FAMILY_SIGS:
                    if sig in buf:
                        family = fam
                        break
                if family:
                    break
                tail = buf[-64:]   # 跨块保留重叠，避免特征串被切断
    except OSError:
        pass
    if family:
        return {"family": family, "label": _FAMILY_LABEL[family],
                "args": list(_FAMILY_ARGS.get(family, [])), "confident": True}
    return {"family": "unknown", "label": _FAMILY_LABEL["unknown"],
            "args": [], "confident": False}


def silent_plan(path, itype=""):
    """给出“怎么装这个包”的方案：exe + 参数 + 是否可靠静默。"""
    info = detect_installer(path)
    if info["family"] == "unknown":
        # 二进制认不出来时，退回 winget 清单声明的类型
        fam = _ITYPE_MAP.get((itype or "").lower())
        if fam in _FAMILY_ARGS:
            info = {"family": fam, "label": _FAMILY_LABEL[fam],
                    "args": list(_FAMILY_ARGS[fam]), "confident": True}
        elif fam in ("msi", "msix"):
            info = {"family": fam, "label": _FAMILY_LABEL[fam],
                    "args": [], "confident": True}

    if info["family"] == "msi":
        return {"exe": "msiexec", "args": ["/i", path, "/qn", "/norestart"],
                "info": info, "confident": True}
    if info["family"] == "zip":
        # 没有可执行的安装程序：由 install_zip_app 负责解压覆盖
        return {"exe": "", "args": [], "info": info, "confident": True}
    if info["family"] == "msix":
        return {"exe": "powershell",
                "args": ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                         "Add-AppxPackage -Path '%s' -ForceApplicationShutdown" % path],
                "info": info, "confident": True}
    if info["confident"]:
        return {"exe": path, "args": list(info["args"]),
                "info": info, "confident": True}
    # 厂商自研封装（如腾讯 Hummer）：没有公开可靠参数。
    # 先按通用 /S 试一次，不行就交给界面/人工处理。
    return {"exe": path, "args": ["/S"], "info": info, "confident": False}


def build_elevated_install_script(needs, out_path, timeout=900):
    """生成「一次 UAC 批量静默安装」的 PowerShell 脚本文本。

    needs: [(idx, app, pkg), ...]，pkg 里有 path / itype。
    每个安装都用自身框架对应的静默参数，并带 WaitForExit 超时：
    超时（-2）说明它停在界面等人点，上层转「需手动」而不是无限挂住。
    返回 (脚本文本, {idx: plan})。
    """
    lines = ["$ErrorActionPreference='Continue'",
             "$of = %s" % _ps_str(out_path),
             "$to = %d" % (timeout * 1000)]
    plans = {}
    for idx, _a, pkg in needs:
        path = pkg.get("path") or ""
        plans[idx] = silent_plan(path, pkg.get("itype") or "")

    msix_items = [(i, a, p) for i, a, p in needs
                  if plans[i]["exe"].lower() == "powershell"]
    if msix_items:
        # MSIX 专用小工具：先关掉旧包正在跑的进程（否则必报 0x80073D02）
        lines.append(
            "function Close-PkgApps($names,$exes,$aggressive){\n"
            "  $locs=@()\n"
            "  foreach($n in $names){ $locs += @(Get-AppxPackage -Name $n | "
            "ForEach-Object { $_.InstallLocation }) }\n"
            "  $skip=@('openconsole.exe','conhost.exe')\n"
            "  $hit=@(Get-Process | Where-Object {\n"
            "    $p=$_\n"
            "    if(-not $p.Path){ return $false }\n"
            "    if($p.Id -eq $PID){ return $false }\n"
            "    $nm=($p.ProcessName.ToLower()+'.exe')\n"
            "    if((-not $aggressive) -and ($skip -contains $nm)){ return $false }\n"
            "    if($exes -contains $nm){ return $true }\n"
            "    foreach($l in $locs){ if($l -and $p.Path.StartsWith($l,"
            "[System.StringComparison]::OrdinalIgnoreCase)){ return $true } }\n"
            "    return $false\n"
            "  })\n"
            "  foreach($p in $hit){ & taskkill /F /T /PID $p.Id *> $null }\n"
            "  return $hit.Count\n"
            "}")

    for idx, a, pkg in needs:
        path = pkg.get("path") or ""
        plan = plans[idx]

        if plan["exe"].lower() == "powershell":
            # MSIX：关旧进程 → Add-AppxPackage → 被占用就加重关一次重试
            info = msix_identity(path)
            names = _uniq(info["names"] + _pkg_names_hint(a))
            exes = _uniq([e.lower() for e in info["exes"]])
            lines.append(
                "$names=@(%s); $exes=@(%s); $msg=''"
                % (", ".join(_ps_str(n) for n in names),
                   ", ".join(_ps_str(e) for e in exes)))
            lines.append(
                "$null = Close-PkgApps $names $exes $false; Start-Sleep -Milliseconds 600; "
                "try { Add-AppxPackage -Path %s -ForceApplicationShutdown "
                "-ErrorAction Stop; $c = 0 } catch { $c = 1; $msg = $_.Exception.Message }"
                % _ps_str(path))
            lines.append(
                "if($c -ne 0 -and $msg -match '80073d02'){ "
                "$null = Close-PkgApps $names $exes $true; Start-Sleep -Milliseconds 1200; "
                "try { Add-AppxPackage -Path %s -ForceApplicationShutdown "
                "-ErrorAction Stop; $c = 0; $msg = '' } "
                "catch { $c = 1; $msg = $_.Exception.Message } }" % _ps_str(path))
            lines.append(
                "\"RESULT|%d|$c\" | Out-File -Append -FilePath $of -Encoding utf8" % idx)
            lines.append(
                "if($msg){ (\"MSG|%d|\" + ($msg -replace '\\s+',' ')) | "
                "Out-File -Append -FilePath $of -Encoding utf8 }" % idx)
            continue

        if not plan["confident"]:
            # 厂商自研封装没有静默参数：在提权脚本里硬跑只会弹个界面干等，
            # 直接标「需手动」，让界面提示用户单独用"依次安装"点掉
            lines.append(
                "\"RESULT|%d|-5\" | Out-File -Append -FilePath $of -Encoding utf8"
                % idx)
            continue

        if is_zip(path):
            # ZIP 包不需要管理员权限，能走到这里通常是别的原因；
            # 兜底交给 winget 重装（绝不能再把 zip 当 exe 启动）
            pid = (a.get("id") or "").strip()
            if pid:
                lines.append(
                    "try { $p = Start-Process -FilePath 'winget.exe' -ArgumentList "
                    "@('install','--id',%s,'-e','--force','--accept-package-agreements',"
                    "'--accept-source-agreements','--disable-interactivity') -PassThru "
                    "-NoNewWindow -ErrorAction Stop; "
                    "if ($p.WaitForExit($to)) { $c = $p.ExitCode } "
                    "else { & taskkill /F /T /PID $p.Id *> $null; $c = -2 } } "
                    "catch { $c = -4 }; "
                    "\"RESULT|%d|$c\" | Out-File -Append -FilePath $of -Encoding utf8"
                    % (_ps_str(pid), idx))
            else:
                lines.append(
                    "\"RESULT|%d|1\" | Out-File -Append -FilePath $of -Encoding utf8" % idx)
            continue

        exe = "msiexec.exe" if plan["exe"].lower() == "msiexec" else plan["exe"]
        arr = ", ".join(_ps_str(x) for x in plan["args"])
        lines.append(
            "try { $p = Start-Process -FilePath %s -ArgumentList @(%s) "
            "-PassThru -ErrorAction Stop; "
            "if ($p.WaitForExit($to)) { $c = $p.ExitCode } "
            "else { & taskkill /F /T /PID $p.Id *> $null; $c = -2 } } "
            "catch { $c = -4 }; "
            "\"RESULT|%d|$c\" | Out-File -Append -FilePath $of -Encoding utf8"
            % (_ps_str(exe), arr, idx))
    return "\n".join(lines), plans


def _kill_tree(pid):
    """强杀进程树（安装器常会派生解压子进程，只杀父进程会残留）。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


_TH32CS_SNAPPROCESS = 0x2


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def _process_tree_pids(root):
    """返回 root 及其所有子孙进程 pid。

    安装器经常把真正的界面跑在派生的子进程里（实测：Win11 记事本由 12256
    派生出 14728，窗口在 14728 上），只看主进程会漏判。
    """
    pids = {root}
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0):
            return pids
        parent = {}
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        try:
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parent[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        changed = True
        while changed:
            changed = False
            for pid, ppid in parent.items():
                if ppid in pids and pid not in pids:
                    pids.add(pid)
                    changed = True
    except Exception:
        pass
    return pids


def _visible_window_title(pid):
    """返回该进程（含子进程）第一个可见窗口标题；没有则 None。"""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return None
    tree = _process_tree_pids(pid)
    hit = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lp):
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(wpid))
        if wpid.value in tree and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            n = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 1)
                hit.append(buf.value)
                return False
        return True

    try:
        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        return None
    return hit[0] if hit else None


def kill_blocking(app, log=print):
    """安装前结束该程序自身的进程，避免“文件被占用 / 无法写入”。"""
    target = (app.get("install_location") or "").strip()
    if not target and app.get("exe"):
        target = os.path.dirname(app["exe"])
    if not target or not os.path.isdir(target):
        return 0
    ps = ("$p=%s; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
          "$_.ExecutablePath.StartsWith($p,'OrdinalIgnoreCase') } | "
          "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; "
          "Write-Output $_.Name } catch {} }" % _ps_str(target))
    rc, out, _err = _run_capture(["powershell", "-NoProfile", "-Command", ps], 60)
    names = [l.strip() for l in (out or "").splitlines() if l.strip()]
    if names:
        log("  已关闭占用进程: %s" % "、".join(sorted(set(names))))
    return len(names)


# ---------------- MSIX / APPX 应用包升级 ----------------
#
# 实测（Windows 终端 1.24.12741 → 1.25.2733）：
#   Add-AppxPackage 直接报 0x80073D02「无法安装，因为需要关闭以下应用：
#   Microsoft.WindowsTerminal_1.24.12741.0_x64__8wekyb3d8bbwe」。
#   这不是权限问题，是 MSIX 的硬性规定：旧包里的程序还在跑，就不许覆盖。
#   -ForceApplicationShutdown 只负责"装完之后把应用关掉"，管不了"装之前先退出"。
#
# 坑在于 OpenConsole.exe：它既是 Windows 终端的控制台宿主，也是**任何**控制台
# 程序的宿主——实测本机就同时有两个 OpenConsole 在托管别的会话（其中一个还是
# 本程序的黑窗口）。所以绝不能按 exe 名字一把全杀，否则等于自己把自己关掉。
# 规则：只杀「旧包 manifest 里注册成应用的 exe」（终端=WindowsTerminal.exe）
# 以及装在包目录下的进程，并跳过自己、自己的祖先、自己的子进程和控制台宿主。

_MSIX_EXT = (".msix", ".msixbundle", ".appx", ".appxbundle")

# 文件头 → 真实容器类型。扩展名不可靠：Epic Online Services 的下载地址是
# `.../download?archive_id=870`，URL 里根本没有文件名，直接被存成 `cache\download`
# （无扩展名）→ 识别全线失效，最后被当 exe 执行，报 WinError 193。
_KIND_EXT = {"exe": ".exe", "msi": ".msi", "msix": ".msix", "zip": ".zip",
             "7z": ".7z"}
_KIND_EXTS = {"zip": {".zip"}, "exe": {".exe"}, "msi": {".msi"},
              "7z": {".7z"},
              "msix": {".msix", ".appx", ".msixbundle", ".appxbundle"}}
_PKG_EXT = (".exe", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle",
            ".zip", ".7z")


def sniff_kind(path):
    """按文件头判断真实类型，不信任扩展名（返回 zip/msix/exe/msi/7z/""）。

    ZIP 和 MSIX 都是 PK 头，靠"包里有没有 AppxManifest.xml"区分。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return ""
    if head[:2] == b"MZ":
        return "exe"
    if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "msi"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(n.lower().strip("/") for n in z.namelist()[:500])
            if ("appxmetadata/appxbundlemanifest.xml" in names
                    or "appxmanifest.xml" in names):
                return "msix"
        except Exception:
            pass
        return "zip"
    return ""


def is_msix(path):
    if not path or not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() in _MSIX_EXT:
        return True
    # 没有扩展名时按内容认
    return (not os.path.splitext(path)[1]) and sniff_kind(path) == "msix"


def _uniq(seq):
    out, seen = [], set()
    for x in seq:
        k = (x or "").lower()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


_msix_cache = {}


def msix_identity(path):
    """读 MSIX/APPX 清单，返回 {"names": [包标识名], "exes": [应用 exe], "version"}。

    bundle 是套娃：外层 AppxBundleManifest.xml 给出包名，内层每个 .msix 里才有
    AppxManifest.xml（Applications/Application Executable=真正会跑的那个 exe）。
    """
    try:
        absp = os.path.abspath(path)
        key = (absp, os.path.getmtime(absp))
    except OSError:
        return {"names": [], "exes": [], "version": ""}
    if key in _msix_cache:
        return _msix_cache[key]

    names, exes = [], []
    ver = []

    def _scan(xml):
        for m in re.finditer(r"<Identity\b[^>]*>", xml):
            tag = m.group(0)
            n = re.search(r'\bName="([^"]+)"', tag)
            if n:
                names.append(n.group(1))
            v = re.search(r'\bVersion="([^"]+)"', tag)
            if v:
                ver.append(v.group(1))
        for m in re.finditer(r"<Application\b[^>]*>", xml):
            e = re.search(r'\bExecutable="([^"]+)"', m.group(0))
            if e:
                exes.append(os.path.basename(e.group(1).replace("\\", "/")))

    try:
        with zipfile.ZipFile(absp) as z:
            files = z.namelist()
            bm = "AppxMetadata/AppxBundleManifest.xml"
            if bm in files:
                _scan(z.read(bm).decode("utf-8", "replace"))
                for inner in [n for n in files if n.lower().endswith((".msix", ".appx"))]:
                    try:
                        with zipfile.ZipFile(io.BytesIO(z.read(inner))) as z2:
                            _scan(z2.read("AppxManifest.xml").decode("utf-8", "replace"))
                    except Exception:
                        continue
            elif "AppxManifest.xml" in files:
                _scan(z.read("AppxManifest.xml").decode("utf-8", "replace"))
    except Exception:
        pass

    ver.sort(reverse=True)
    out = {"names": _uniq(names), "exes": _uniq(exes),
           "version": ver[0] if ver else ""}
    _msix_cache[key] = out
    return out


def _parent_map():
    """{pid: 父pid} 快照，用于算祖先进程。"""
    parent = {}
    try:
        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0):
            return parent
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        try:
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parent[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return parent


def _console_host_pid():
    """本进程控制台窗口的宿主 pid（可能是 conhost.exe，也可能是 OpenConsole.exe）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetConsoleWindow()
        if not hwnd:
            return 0
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _protected_pids():
    """安装前绝对不能杀的进程：自己、祖先、子进程、自己的控制台宿主。"""
    me = os.getpid()
    pm = _parent_map()
    pids = {me}
    p = me
    for _ in range(32):
        p = pm.get(p)
        if not p or p in pids:
            break
        pids.add(p)
    try:
        pids |= _process_tree_pids(me)
    except Exception:
        pass
    host = _console_host_pid()
    if host:
        pids.add(host)
    return pids


# 共享控制台宿主：只有"确实还挡着安装"时才在第二轮里动它
_MSIX_SHARED_HOSTS = ("openconsole.exe", "conhost.exe")


def _pkg_names_hint(app):
    """从 winget id / 显示名里猜包名（Microsoft.WindowsTerminal 这类）。"""
    out = []
    for cand in ((app or {}).get("id"), (app or {}).get("name")):
        cand = (cand or "").strip()
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]{2,}$", cand) and "." in cand:
            out.append(cand)
    return out


def close_package_apps(path="", app=None, names=(), exes=(), log=print,
                       aggressive=False, wait=0.6):
    """关掉占着旧 MSIX 包的应用进程，返回被关掉的进程名列表。

    aggressive=False 时跳过 OpenConsole/conhost 这类共享控制台宿主；
    只有在"关完还被 0x80073D02 挡住"的第二轮才允许动它们。
    """
    if is_msix(path):
        info = msix_identity(path)
        names = list(names) + info["names"]
        exes = list(exes) + info["exes"]
    names = _uniq(list(names) + _pkg_names_hint(app))
    exes = _uniq([e for e in exes if e.lower().endswith(".exe")])
    if not names and not exes:
        return []

    protect = sorted(_protected_pids())
    skip = [] if aggressive else list(_MSIX_SHARED_HOSTS)
    ps = "\n".join([
        "$ErrorActionPreference='SilentlyContinue'",
        "$names=@(%s)" % ", ".join(_ps_str(n) for n in names),
        "$exes=@(%s)" % ", ".join(_ps_str(e.lower()) for e in exes),
        "$protect=@(%s)" % ", ".join(str(p) for p in protect),
        "$skip=@(%s)" % ", ".join(_ps_str(s) for s in skip),
        "$locs=@()",
        "foreach($n in $names){ $locs += @(Get-AppxPackage -Name $n | "
        "ForEach-Object { $_.InstallLocation }) }",
        "$locs=@($locs | Where-Object { $_ })",
        "$hit=@(Get-Process | Where-Object {",
        "  $p=$_",
        "  if(-not $p.Path){ return $false }",
        "  if($protect -contains $p.Id){ return $false }",
        "  $nm=($p.ProcessName.ToLower()+'.exe')",
        "  if($skip -contains $nm){ return $false }",
        "  if($exes -contains $nm){ return $true }",
        "  foreach($l in $locs){ if($p.Path.StartsWith($l,"
        "[System.StringComparison]::OrdinalIgnoreCase)){ return $true } }",
        "  return $false",
        "})",
        "foreach($p in $hit){ 'KILL|'+$p.ProcessName+'|'+$p.Id; "
        "& taskkill /F /T /PID $p.Id *> $null }",
        "'DONE'",
    ])
    try:
        _rc, out, _err = _run_capture(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], 90)
    except Exception as e:
        log("  关闭占用进程出错（忽略）: %r" % e)
        return []
    killed = []
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("KILL|"):
            parts = line.split("|")
            if len(parts) >= 2 and parts[1]:
                killed.append(parts[1])
    killed = _uniq(killed)
    if killed:
        log("  已关闭正在运行的进程: %s（不关的话 MSIX 会报 0x80073D02）"
            % "、".join(killed))
        if wait:
            time.sleep(wait)
    return killed


def _appx_version(names):
    """读已安装的 MSIX 版本号；读不到返回空串。"""
    if not names:
        return ""
    ps = ("$n=@(%s); $n | ForEach-Object { Get-AppxPackage -Name $_ } | "
          "Sort-Object Version -Descending | "
          "Select-Object -First 1 -ExpandProperty Version"
          % ", ".join(_ps_str(n) for n in names))
    _rc, out, _err = _run_capture(
        ["powershell", "-NoProfile", "-Command", ps], 40)
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


# MSIX 安装常见 HRESULT → (状态, 中文原因)
_APPX_HR = {
    "80073d02": ("failed", "旧版本还在运行，MSIX 必须先退出它才能覆盖"),
    "80073d06": ("not_applicable", "系统里已装更新的版本，无需升级"),
    "80073cfb": ("not_applicable", "已安装相同版本，无需重复安装"),
    "80073cf3": ("failed", "缺少依赖包（VCLibs / WinUI 等），请先在应用商店装好该应用再升级"),
    "80073cf9": ("failed", "写入失败：系统盘空间不足或 WindowsApps 目录异常"),
    "80073cfe": ("failed", "包签名或发布者不匹配，请使用官方安装包"),
    "80070005": ("permission", "系统拒绝写入，需要管理员权限"),
}


def classify_msix(rc, text):
    """把 MSIX 安装结果翻译成中文。"""
    text = text or ""
    low = text.lower()
    if rc == 0:
        return _result(True, "success", "")
    for code, (status, reason) in _APPX_HR.items():
        if code in low:
            return _result(False, status, reason + "（0x%s）" % code.upper())
    if rc == -2147009278:
        return _result(False, "failed", _APPX_HR["80073d02"][1])
    # 兜底：抓一行有意义的信息
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("At line") or line.startswith("+"):
            continue
        if any(k in line.lower() for k in ("error", "错误", "失败", "无法", "0x80")):
            return _result(False, "failed", "MSIX 安装失败：%s" % line[:160])
    return _result(False, "failed", "MSIX 安装失败（退出码 %s）%s"
                   % (rc, ("：" + text.strip()[:160]) if text.strip() else ""))


def install_msix(path, app, log=print, progress=None, cancel=None, timeout=900):
    """安装 MSIX / APPX 包：自动退出旧版本 → 系统接口登记安装 → 失败重试一次。

    走 Add-AppxPackage 而不是双击安装包，是因为双击走 App Installer 界面，
    一样会被"应用正在运行"挡住，而且还得人手点。
    """
    name = (app or {}).get("name") or (app or {}).get("id") or ""
    if not path or not os.path.exists(path):
        return _result(False, "failed", "安装包不存在：%s" % path)
    _emit(progress, phase="install", app=name, percent=None)

    info = msix_identity(path)
    names, exes = info["names"], info["exes"]
    if not names:
        names = _pkg_names_hint(app)
    log("MSIX 应用包 → 用系统接口登记安装（不需要点界面）")
    if names:
        log("包标识: %s%s" % ("、".join(names),
                            ("　应用: " + "、".join(exes)) if exes else ""))
    before = _appx_version(names)
    if before:
        log("当前版本: %s" % before)

    # ① 先请旧版本退出——这是 0x80073D02 的唯一解法
    close_package_apps(path, app, log=log)

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
           "Add-AppxPackage -Path '%s' -ForceApplicationShutdown" % path]
    rc, out, err = 0, "", ""
    text = ""
    for attempt in (1, 2):
        if cancel and cancel():
            return _result(False, "cancelled", "已取消")
        log("  登记安装（第 %d 次）..." % attempt)
        rc, out, err = _run_capture(cmd, timeout)
        text = ((out or "") + "\n" + (err or "")).strip()
        if rc == 0 or "80073d02" not in text.lower():
            break
        if attempt == 1:
            log("  仍被“应用正在运行”挡住（0x80073D02）→ 连控制台宿主一起关掉再试一次")
            close_package_apps(path, app, log=log, aggressive=True, wait=1.5)

    after = _appx_version(names)
    if rc == 0:
        if before and after and before != after:
            log("  版本: %s -> %s" % (before, after))
        elif after:
            log("  当前版本: %s" % after)
        _emit(progress, phase="install", app=name, percent=100.0)
        return _result(True, "success", "")

    if before and after and before != after:
        log("  安装器报错，但版本已变成 %s，按成功处理" % after)
        _emit(progress, phase="install", app=name, percent=100.0)
        res = _result(True, "success", "")
        res["reason"] = "系统返回错误但版本已更新为 %s（按成功计）" % after
        return res

    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("At line", "+", "    ")):
            log("  " + line[:200])
    res = classify_msix(rc, text)
    if "80073d02" in (res.get("reason") or "").lower():
        # 自动关都关了还被挡：十有八九是应用在别的用户会话/管理员窗口里跑着
        res["reason"] += ("；已自动关闭但没关干净，请手动退出「%s」的所有窗口后再试"
                          % (name or "该应用"))
    return res


def launch_installer_visible(path, log=print):
    """打开安装包（会弹 UAC / 安装界面），交给用户点完——最后的兜底手段。"""
    try:
        os.startfile(path)
    except OSError as e:
        log("  打开安装包失败: %r" % e)
        return False
    log("  已打开安装包，请在弹出的界面里点完成：%s" % os.path.basename(path))
    return True


# ---------------- 交互式安装：打开安装包 → 用户点完 → 用版本号核实 ----------------
#
# 这是默认安装方式，原因是"猜静默参数"这条路不可靠：
#   - NSIS 不认识 /quiet，只会忽略它然后照常弹界面 → 表现为"安装不动"
#   - 厂商自研封装（腾讯 Hummer 之类）根本没有公开参数
#   - 猜错时安装器还可能返回非 0，但其实已经装好了 → 表现为"提示失败但实际装上了"
#
# 所以改成：打开安装包让用户点，然后**用注册表/文件版本号判定结果**。
# 版本变了就是成功了，跟安装器返回什么退出码无关。

SEE_MASK_NOCLOSEPROCESS = 0x00000040
STILL_ACTIVE = 259
ERROR_CANCELLED = 1223

# 单个安装包最多等多久（用户可能中途去干别的）
INTERACTIVE_TIMEOUT = 1800
# 安装器进程退出后，再等多久确认版本落地（它常分几阶段写注册表）
# 不宜太长：有些程序装完版本号本来就不变，等太久只会让用户干等
VERIFY_TIMEOUT = 45


class _SHELLEXECUTEINFOW(ctypes.Structure):
    """ShellExecuteEx 的参数结构（按 64 位对齐，ctypes 会自动补齐 padding）。"""
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _handle_pid(h):
    try:
        return int(ctypes.windll.kernel32.GetProcessId(ctypes.c_void_p(h)))
    except Exception:
        return 0


def _exit_code(hp):
    code = ctypes.c_ulong()
    try:
        ok = ctypes.windll.kernel32.GetExitCodeProcess(
            hp, ctypes.byref(code))
    except Exception:
        return -4
    if not ok:
        return -4
    v = int(code.value) & 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def _pid_alive(pid):
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        return _exit_code(ctypes.c_void_p(h)) == STILL_ACTIVE
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _close_handle(h):
    try:
        if h:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass


def launch_installer_ex(path, log=print, parameters=""):
    """打开安装包并拿到它的进程句柄（需要管理员时由系统自己弹 UAC）。

    用 ShellExecuteEx 而不是 subprocess 的原因：带 requireAdministrator 清单的
    安装包直接 CreateProcess 会抛 WinError 740，走 shell 打开则系统自动提权。
    返回 (hProcess, pid, err)，err 为 ""/"elevation_cancelled"/"open_failed"。
    """
    if os.name != "nt":
        log("  仅 Windows 支持打开安装包")
        return None, 0, "open_failed"
    if not path or not os.path.exists(path):
        log("  安装包不存在: %s" % path)
        return None, 0, "open_failed"
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    except Exception as e:
        log("  加载 shell32 失败: %r" % e)
        return None, 0, "open_failed"
    fn = shell32.ShellExecuteExW
    fn.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    fn.restype = ctypes.c_int

    def _run(verb):
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = verb
        sei.lpFile = path
        sei.lpParameters = parameters or None
        sei.lpDirectory = os.path.dirname(path) or None
        sei.nShow = 1   # SW_SHOWNORMAL
        if fn(ctypes.byref(sei)):
            return sei.hProcess
        return None

    # 先走 open，交给系统按安装包清单决定要不要提权
    h = _run("open")
    if h:
        pid = _handle_pid(h)
        log("  已打开安装包: %s" % os.path.basename(path))
        return h, pid, ""
    last_err = ctypes.get_last_error()
    if last_err == ERROR_CANCELLED:
        log("  已取消管理员授权（UAC）")
        return None, 0, "elevation_cancelled"
    if last_err in (2, 3):
        log("  安装包路径不存在: %s" % path)
        return None, 0, "open_failed"

    # open 被拒但文件在：用 runas 强制提权再试一次
    h = _run("runas")
    if h:
        pid = _handle_pid(h)
        log("  已以管理员身份打开安装包: %s" % os.path.basename(path))
        return h, pid, ""
    last_err = ctypes.get_last_error()
    if last_err == ERROR_CANCELLED:
        log("  已取消管理员授权（UAC）")
        return None, 0, "elevation_cancelled"
    log("  打开安装包失败（win32=%d）: %s" % (last_err, path))
    return None, 0, "open_failed"


def wait_process(hproc, pid=0, log=print, cancel=None, timeout=INTERACTIVE_TIMEOUT,
                 tick=300):
    """等安装器进程退出，期间记下它派生过的整棵进程树。

    记进程树是为了判断"真装完"：安装器常先解压再启动内层安装程序，
    外层秒退，真正在干活的是内层。返回 (rc, note, tree)。note 为
    ""/"cancelled"/"timeout"。cancel 只停止等待，**不杀**安装器——用户可能正装到一半。
    """
    k32 = ctypes.windll.kernel32
    hp = ctypes.c_void_p(hproc)
    start = time.time()
    tree = set()
    last_scan = 0.0
    while True:
        try:
            w = k32.WaitForSingleObject(hp, tick)
        except Exception:
            return -4, "", tree
        if w == 0:
            return _exit_code(hp), "", tree
        now = time.time()
        if pid and now - last_scan > 2.0:
            last_scan = now
            try:
                tree |= _process_tree_pids(pid)
            except Exception:
                pass
        if cancel and cancel():
            return None, "cancelled", tree
        if now - start > timeout:
            return None, "timeout", tree


def _norm_name(s):
    """把程序名归一化后比较（去掉空格/连字符/括号内容，统一小写）。"""
    s = (s or "").lower()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s\-_·.]+", "", s)
    return s.strip()


def _reg_query(key, name):
    try:
        import winreg
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _registry_version(name):
    """按显示名在卸载注册表里查当前版本（只读两个值，比全量扫描快得多）。"""
    try:
        import winreg
    except ImportError:
        return ""
    want = _norm_name(name)
    if not want:
        return ""
    exact = ""
    fuzzy = ""
    fuzzy_len = 10 ** 6
    for root, path, flag in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0)):
        try:
            key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        try:
            total = winreg.QueryInfoKey(key)[0]
        except OSError:
            key.Close()
            continue
        for i in range(total):
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                continue
            try:
                sk = winreg.OpenKey(key, sub)
            except OSError:
                continue
            try:
                dn = str(_reg_query(sk, "DisplayName") or "")
                if not dn:
                    continue
                nd = _norm_name(dn)
                if nd != want and want not in nd and nd not in want:
                    continue
                ver = str(_reg_query(sk, "DisplayVersion")
                          or _reg_query(sk, "Version") or "").strip()
                if not ver:
                    continue
                if nd == want:
                    if not exact or version.is_newer(ver, exact):
                        exact = ver
                elif len(nd) < fuzzy_len:
                    fuzzy, fuzzy_len = ver, len(nd)
            finally:
                sk.Close()
        key.Close()
        if exact:
            return exact
    return exact or fuzzy


def _exe_version(path):
    """读主程序文件里写的版本号（注册表查不到时兜底）。"""
    if not path or not os.path.exists(path):
        return ""
    try:
        import scanner
        vi = scanner.get_version_info(path) or {}
        return str(vi.get("ProductVersion") or vi.get("FileVersion") or "").strip()
    except Exception:
        return ""


def installed_version(app):
    """该程序当前真实版本：注册表优先，取不到再读主程序文件版本。"""
    v = _registry_version(app.get("name") or "")
    if v:
        return v
    return _exe_version(app.get("exe") or "")


def _version_moved(before, after):
    """版本是否真的变了（before 读不到时，只要 after 有值就算变了）。"""
    if not after:
        return False
    if not before:
        return True
    return not version.same(before, after)


def _await_version(app, before, log=print, timeout=VERIFY_TIMEOUT, cancel=None,
                   extra_pids=None):
    """等版本真正落地。内层安装进程还在跑就继续等，别急着下结论。"""
    start = time.time()
    last = before
    while time.time() - start < timeout:
        if cancel and cancel():
            break
        if extra_pids:
            alive = [p for p in extra_pids if _pid_alive(p)]
            if alive:
                time.sleep(2.0)
                continue
        last = installed_version(app)
        if _version_moved(before, last):
            return last
        time.sleep(1.5)
    return last


def install_interactive(app, pkg, log=print, progress=None, cancel=None,
                        timeout=INTERACTIVE_TIMEOUT, verify_timeout=VERIFY_TIMEOUT):
    """打开安装包让用户点完，再用版本号核实是否真装上。

    这是默认安装方式：不猜静默参数，安装包自带的管理员清单由系统弹 UAC。
    结果判定看**版本号**而不是安装器退出码——安装器返回非 0 但其实装好了很常见。
    """
    name = app.get("name") or app.get("id") or ""
    path = pkg.get("path")
    if not path or not os.path.exists(path):
        return _result(False, "failed", "安装包不存在：%s" % path)
    if is_msix(path):
        # MSIX 没有"点几下就好"这回事：装的时候旧版本必须在跑之外，
        # 所以这里直接交给 install_msix（自动关旧进程 + 系统接口安装）
        log("这是 MSIX 应用包，安装前必须先退出旧版本 → 自动处理，不用点界面")
        return install_msix(path, app, log=log, progress=progress, cancel=cancel)
    if is_zip(path):
        if _zip_nested_installer(path, app):
            # 压缩包里带厂商安装程序 → 打开它，等你点完
            return _install_nested_zip(path, app, log=log, progress=progress,
                                       cancel=cancel, interactive=True)
        log("这是 ZIP 绿色版包，没有安装界面 → 自动解压覆盖")
        return install_zip_app(path, app, log=log, progress=progress, cancel=cancel)
    _emit(progress, phase="install", app=name, percent=None)

    # 这里**不主动结束**程序自身的进程：用户可能正在用（聊天、编辑文档），
    # 安装包界面自己会提示"请先关闭程序"。静默安装那边才需要先杀掉。

    before = installed_version(app)
    log("安装包: %s" % path)
    log("当前版本: %s" % (before or "（读不到，不影响安装）"))

    want = (pkg.get("sha256") or "").strip().lower() if isinstance(pkg, dict) else ""
    bad_hash = False
    if want:
        got = _sha256(path)
        if got and got != want:
            bad_hash = True
            log("  提示：安装包与官方清单哈希不一致，可能下载不完整（仍会打开）")

    hproc, pid, err = launch_installer_ex(path, log)
    if not hproc:
        if err == "elevation_cancelled":
            return _result(False, "cancelled", "你在 UAC 里点了“否”，已跳过")
        return _result(False, "failed", "无法打开安装包（可能被安全软件拦截）")

    if timeout:
        log("请在弹出的安装界面里点完（关掉窗口就算完成，会自动继续下一个）")
        log("  最多等 %d 分钟，超时会跳过这一个。" % max(1, timeout // 60))

    rc, note, tree = wait_process(hproc, pid, log, cancel=cancel, timeout=timeout)
    _close_handle(hproc)

    if note == "cancelled":
        return _result(False, "cancelled",
                       "已停止等待（安装窗口没关的话可以继续点完）")
    if note == "timeout":
        return _result(False, "manual",
                       "安装界面开了 %d 分钟还没结束，已跳过"
                       % max(1, (timeout or 0) // 60))

    log("  安装器已结束（退出码 %d），正在核对版本..." % rc)
    tree.discard(pid)
    after = _await_version(app, before, log, verify_timeout, cancel, tree)

    if _version_moved(before, after):
        log("  版本: %s -> %s" % (before or "?", after))
        _emit(progress, phase="install", app=name, percent=100.0)
        res = _result(True, "success", "")
        if rc not in (0, 3010):
            res["reason"] = "安装器退出码 %d，但版本已更新为 %s（按成功计）" % (rc, after)
            log("  （退出码看着像失败，实际已装好，按成功处理）")
        return res

    if not before and not after:
        if rc in (0, 3010):
            return _result(True, "success", "")
        return _result(False, "manual",
                       "安装器返回 %s，但读不到该程序版本，请在列表里确认是否已更新"
                       % rc)

    res = classify_install(rc, "")
    if res["ok"]:
        res["reason"] = "版本号未变化（可能安装器没写入版本信息），请确认"
        return res
    reason = res.get("reason") or ""
    if before:
        reason = (reason + "｜版本仍是 %s，未更新" % before).lstrip("｜")
    if bad_hash:
        reason = (reason + "｜安装包哈希与官方不一致，建议删除缓存重下").lstrip("｜")
    res["reason"] = reason
    return res


def winget_cmd_candidates(path, itype):
    """返回安装命令 [(exe, [args...]), ...]。

    保留多候选的返回形状以兼容旧调用方，但实际只给一个"按框架判定"的方案——
    多参数轮流盲试会把安装界面反复弹出来，反而更糟。
    """
    plan = silent_plan(path, itype)
    return [(plan["exe"], list(plan["args"]))]


def _run_install(cmd, log, cancel=None, timeout=900, gui_watch=True):
    """运行安装命令并返回 (rc, 合并输出文本)。

    rc 约定：
      740  安装包自带 requireAdministrator 清单，普通权限下系统直接拒绝启动
      -2   超时（很可能停在安装界面等人点）
      -3   用户取消
    """
    log("执行: " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        winerr = getattr(e, "winerror", None)
        msg = str(e)
        # WinError 740 = ERROR_ELEVATION_REQUIRED：安装包自身要求管理员权限
        if winerr == 740 or "需要提升" in msg or "requires elevation" in msg.lower() \
                or "elevation required" in msg.lower():
            return 740, msg
        return None, msg

    q_lines = queue.Queue()

    def _reader():
        try:
            for raw in iter(p.stdout.readline, b""):
                q_lines.put(raw)
        except Exception:
            pass
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()

    out = []
    start = time.time()
    hinted = False
    rc = None
    while True:
        drained = False
        try:
            while True:
                raw = q_lines.get_nowait()
                drained = True
                for piece in re.split(rb"[\r\n]", raw):
                    line = decode(piece).strip()
                    if line:
                        log("  " + line)
                        out.append(line)
        except queue.Empty:
            pass
        code = p.poll()
        if code is not None:
            rc = code
            break
        if cancel and cancel():
            _kill_tree(p.pid)
            return -3, "已取消"
        elapsed = time.time() - start
        if gui_watch and not hinted and elapsed > 8:
            title = _visible_window_title(p.pid)
            if title:
                hinted = True
                log("  检测到安装界面（标题：%s）——该安装器不支持全静默。" % title)
                log("  请在弹出的窗口里点完，本程序会等它结束（%d 秒后转手动）。" % timeout)
        if elapsed > timeout:
            _kill_tree(p.pid)
            log("  已超过 %d 秒仍未结束，强制结束该安装进程。" % timeout)
            return -2, "安装器超时（很可能停在界面等待操作）"
        if not drained:
            time.sleep(0.25)

    # 退出后把残余输出读干净
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            raw = q_lines.get(timeout=0.2)
        except queue.Empty:
            break
        for piece in re.split(rb"[\r\n]", raw):
            line = decode(piece).strip()
            if line:
                log("  " + line)
                out.append(line)

    log("  退出码: %d" % rc)
    return rc, "\n".join(out)



def classify_install(rc, text):
    """把安装退出码 + 输出翻译成中文结果。"""
    text = text or ""
    low = text.lower()
    if rc == -3:
        return _result(False, "cancelled", "已取消")
    if rc == -2:
        return _result(False, "manual",
                       "安装器弹出了界面并等待操作（超时未完成），请手动点完安装")
    if rc == -5:
        return _result(False, "manual",
                       "该程序是厂商自研封装、没有静默参数，请在列表里单独点"
                       "「依次打开安装包安装」逐个点完")
    if rc == -4:
        return _result(False, "failed",
                       "无法启动安装包（文件损坏，或被安全软件拦截）")
    # 把 zip 当 exe 执行时的经典报错，翻译成能看懂的话
    if "不是有效的 win32" in low or "winerror 193" in low \
            or "not a valid win32 application" in low:
        return _result(False, "failed",
                       "这个包是 ZIP 压缩包，不是安装程序（应按绿色版解压覆盖）")
    if rc in (0, 3010):
        return _result(True, "success", "")
    # MSIX「应用正在运行」：不是权限问题，重装多少次都一样，必须先把程序退出
    if rc == -2147009278 or "0x80073d02" in low:
        return _result(False, "failed",
                       "程序正在运行，MSIX 必须先退出旧版本才能覆盖（0x80073D02）；"
                       "请关掉该应用所有窗口后重试")
    if "0x80073d06" in low:
        return _result(False, "not_applicable", "系统里已装更新的版本（0x80073D06）")
    # winget 专用 HRESULT：无可适用升级（已是最新 / 架构不符）
    if rc == -1978335189:
        return _result(False, "not_applicable",
                       "winget：无可适用的升级（已是最新或架构不符）")
    if rc == -1978335212:
        return _result(False, "not_applicable",
                       "winget 清单里找不到这个包的可安装版本")
    if rc in (5, 740) or "拒绝访问" in text or "access is denied" in low \
            or "requires elevation" in low:
        return _result(False, "permission",
                       "系统无法访问文件：需管理员权限，或先彻底关闭该程序")
    if rc in (1605, 1612, 1638):
        return _result(False, "not_applicable",
                       "已安装相同或更高版本（无需重复安装）")
    if rc == 1603:
        return _result(False, "failed",
                       "安装失败(1603)：权限不足，或缺少 .NET / VC++ 等运行库")
    if rc in (1639,):
        return _result(False, "failed", "静默安装参数不被该安装包支持")
    reason = "安装退出码 %d" % rc
    for kw in ("error", "失败", "无法", "拒绝", "denied", "in use"):
        for ln in text.splitlines():
            if kw in ln.lower():
                reason += "：" + ln.strip()[:120]
                break
        else:
            continue
        break
    return _result(False, "failed", reason)


def _install_winget(path, itype, app, log=print, progress=None, cancel=None,
                    timeout=900, pkg=None):
    name = app.get("name") or app.get("id")
    _emit(progress, phase="install", app=name, percent=None)
    if not path or not os.path.exists(path):
        return _result(False, "failed", "安装包不存在：%s" % path)
    if is_zip(path):
        # 兜底：任何路径漏到这里也不能把 zip 当 exe 执行
        return install_zip_app(path, app, log=log, progress=progress, cancel=cancel)

    # 先关掉它自己的进程，避免文件占用（这类失败以前会被误报成权限问题）
    try:
        kill_blocking(app, log)
    except Exception as e:
        log("  关闭占用进程时出错（忽略）: %r" % e)

    plan = silent_plan(path, itype)
    info = plan["info"]
    log("安装包: %s" % path)

    # 装之前先对一次哈希——不完整的包会"一闪就失败"，很难看出原因
    bad_hash = False
    want_sha = (pkg.get("sha256") or "").strip().lower() if isinstance(pkg, dict) else None
    if want_sha:
        try:
            got_sha = _sha256(path)
        except OSError:
            got_sha = ""
        if got_sha and got_sha != want_sha:
            bad_hash = True
            log("  警告：安装包与官方清单哈希不一致，可能下载不完整")
            log("    （清单 %s… / 实际 %s…）" % (want_sha[:16], got_sha[:16]))
    if not plan["confident"]:
        # 厂商自研封装（QQ 的 Hummer、Epic 的 EOS Installer…）：实测把 304MB 的
        # 安装器整个扫完，连 silent/quiet 这类字样都找不到，说明它压根没有静默
        # 参数。硬塞 /S 只会把界面弹出来干等一整轮超时，最后还是"装不上"。
        # 所以不猜参数，直接走"打开安装包 → 你点完 → 版本号核对"。
        log("  安装器: %s（没有公开的静默参数）" % info["label"])
        log("  → 不猜参数，改为打开安装界面；你点完关掉窗口，我按版本号核对结果。")
        return install_interactive(app, {"path": path}, log=log,
                                   progress=progress, cancel=cancel)

    if plan["args"]:
        log("安装器: %s → 静默参数 %s" % (info["label"], " ".join(plan["args"])))
    else:
        log("安装器: %s → 不需要参数" % info["label"])

    rc, text = _run_install([plan["exe"]] + plan["args"], log, cancel,
                            timeout=timeout)
    if rc is None:
        return _result(False, "failed", "无法启动安装程序：" + text)
    res = classify_install(rc, text)
    if res["ok"]:
        _emit(progress, phase="install", app=name, percent=100.0)
        return res
    if bad_hash:
        res["reason"] = (res.get("reason", "") +
                         "｜安装包哈希与官方不一致，建议删除缓存后重新下载")
    return res


# ---------------- 管理员提权安装 ----------------

def _ps_str(s):
    """生成一个单引号包裹的 PowerShell 字符串字面量。"""
    return "'" + (s or "").replace("'", "''") + "'"


def install_elevated(exe, args, log=print, timeout=1800):
    """以管理员身份运行 (exe + args)，返回退出码（会弹出 UAC 确认）。"""
    arr = ", ".join(_ps_str(a) for a in args)
    ps = ("Start-Process -FilePath %s -ArgumentList @(%s) "
          "-Verb RunAs -Wait -PassThru | Select-Object -ExpandProperty ExitCode"
          % (_ps_str(exe), arr))
    rc, out, err = _run_capture(
        ["powershell", "-NoProfile", "-Command", ps], timeout)
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    code = None
    if lines:
        try:
            code = int(lines[-1])
        except ValueError:
            code = None
    if code is None:
        code = rc
    return code


def run_elevated_ps1(ps1_path, log=print, timeout=1800):
    """以管理员身份运行一个 .ps1 脚本（一次 UAC）。"""
    inner = '-NoProfile -ExecutionPolicy Bypass -File "%s"' % ps1_path
    outer = ["powershell", "-NoProfile", "-Command",
             "Start-Process powershell -ArgumentList %s -Verb RunAs -Wait"
             % _ps_str(inner)]
    rc, _out, _err = _run_capture(outer, timeout)
    return rc


def elevated_portable(path, target, app, log=print):
    """以管理员身份把绿色版覆盖到 target（应对 Program Files 之类需提权的目录）。"""
    import tempfile
    ps1 = tempfile.mktemp(suffix=".ps1")
    if zipfile.is_zipfile(path):
        tmp = tempfile.mktemp(prefix="oku-")
        sub = (app.get("from") or "").replace("/", "\\").strip("\\")
        src = "$tmp"
        if sub:
            src = "Join-Path $tmp %s" % _ps_str(sub)
        script = (
            "$ErrorActionPreference='Stop'\n"
            "$zip=%s; $tmp=%s; $dst=%s;\n"
            "Expand-Archive -Path $zip -DestinationPath $tmp -Force;\n"
            "if(Test-Path %s){ Copy-Item -Path (Join-Path %s '*') -Destination $dst -Recurse -Force } "
            "else { Write-Error '压缩包内路径不存在' }\n"
            % (_ps_str(path), _ps_str(tmp), _ps_str(target), src, src)
        )
    else:
        dst = os.path.join(target, os.path.basename(path))
        script = "Copy-Item -Path %s -Destination %s -Force\n" % (
            _ps_str(path), _ps_str(dst))
    try:
        # 脚本里含中文路径，必须带 BOM，否则 PowerShell 5.1 按 GBK 读取会乱码
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(script)
        run_elevated_ps1(ps1, log)
        return _result(True, "success", "已以管理员身份覆盖")
    except Exception as e:
        return _result(False, "failed", "管理员覆盖失败：%s" % e)
    finally:
        _safe_remove(ps1)


# ---------------------------------------------------------------- 绿色版

def _registry_location(name):
    """按显示名从卸载注册表里取 InstallLocation（winget 的 ZIP 包也会写这条）。"""
    try:
        import winreg
    except ImportError:
        return ""
    want = _norm_name(name)
    if not want:
        return ""
    for root, path, flag in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0)):
        try:
            key = winreg.OpenKey(root, path, 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        try:
            total = winreg.QueryInfoKey(key)[0]
        except OSError:
            key.Close()
            continue
        for i in range(total):
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                continue
            try:
                sk = winreg.OpenKey(key, sub)
            except OSError:
                continue
            try:
                dn = str(_reg_query(sk, "DisplayName") or "")
                if not dn:
                    continue
                nd = _norm_name(dn)
                if nd != want and want not in nd and nd not in want:
                    continue
                loc = str(_reg_query(sk, "InstallLocation") or "").strip().strip('"')
                if loc:
                    return loc
            finally:
                sk.Close()
        key.Close()
    return ""


def is_zip(path):
    if not path or not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() == ".zip":
        return True
    # 无扩展名的包（如存成 cache\download 的 Epic）按内容认
    return (not os.path.splitext(path)[1]) and sniff_kind(path) == "zip"


# ---- ZIP 里装着"官方安装程序"的情况 ----
#
# winget 的清单里有 `InstallerType: zip` + `NestedInstallerType/NestedInstallerFiles`
# 这种组合：下载物是压缩包，但压缩包**不是**绿色版，里面躺着一个厂商自己的安装
# 程序。典型就是 Epic Online Services（4.3.1）：
#   URL   https://onlineservices.epicgames.com/api/cosmos/sdk/download?archive_id=870
#   包内  SDK\Tools\EpicOnlineServicesInstaller.exe   （304MB）
#   清单  NestedInstallerFiles: SDK\Tools\EpicOnlineServicesInstaller.exe
# 对这类包，"解压覆盖到安装目录"完全不对——它压根没有"安装目录"这个概念，
# 必须把里面的安装程序取出来跑一遍。
_ZIP_NESTED_TIPS = {
    "epicgames.epiconlineservices": "sdk/tools/epiconlineservicesinstaller.exe",
}
_ZIP_INSTALLER_HINTS = ("installer", "setup", "bootstrap", "bootstrapper")
_ZIP_NOT_INSTALLER = ("unins", "uninst", "crash", "update", "updat", "upgrade",
                      "repair", "helper", "tool")


def _zip_nested_installer(archive, app=None):
    """zip 里若有厂商自带安装程序，返回其包内相对路径；纯绿色版返回 ""。"""
    try:
        with zipfile.ZipFile(archive) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
    except Exception:
        return ""
    if not infos:
        return ""
    pid = ((app or {}).get("id") or "").strip().lower()
    tip = _ZIP_NESTED_TIPS.get(pid, "")
    norm = {i.filename.replace("\\", "/").lower().strip("/"): i.filename
            for i in infos}
    if tip:
        for k, orig in norm.items():
            if k == tip or k.endswith("/" + tip):
                return orig

    best, best_score = "", 0
    for i in infos:
        low = i.filename.replace("\\", "/").lower()
        base = os.path.basename(low)
        ext = os.path.splitext(base)[1]
        if ext not in (".exe", ".msi", ".msix", ".appx"):
            continue
        if any(h in base for h in _ZIP_NOT_INSTALLER):
            continue
        score = 0
        if ext in (".msi", ".msix", ".appx"):
            score += 50
        if any(h in base for h in _ZIP_INSTALLER_HINTS):
            score += 40
        score += min(i.file_size // (20 * 1024 * 1024), 20)   # 越大越像安装程序
        if score > best_score:
            best, best_score = i.filename, score
    # 门槛：必须是"看名字就知道是安装程序"的，否则绿色版（rg.exe 之类）会被误判
    return best if best_score >= 40 else ""


def _extract_member(archive, rel, log=print):
    """把 zip 里的某个文件取到 cache\\_extract\\<包名>\\ 下（下过就直接复用）。"""
    stem = _safe(os.path.splitext(os.path.basename(archive))[0]) or "pkg"
    out_dir = os.path.join(CACHE_DIR, "_extract", stem)
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, os.path.basename(rel.replace("\\", "/")))
    try:
        with zipfile.ZipFile(archive) as z:
            info = None
            for i in z.infolist():
                if i.filename.replace("\\", "/").lower().strip("/") == \
                        rel.replace("\\", "/").lower().strip("/"):
                    info = i
                    break
            if info is None:
                raise KeyError(rel)
            if os.path.isfile(dst) and os.path.getsize(dst) == info.file_size:
                log("  已提取过: %s" % dst)
                return dst
            log("  从压缩包里取出安装程序: %s（%.1f MB）"
                % (rel, info.file_size / 1048576.0))
            tmp = dst + ".part"
            with z.open(info) as src, open(tmp, "wb") as f:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(tmp, dst)
    except Exception as e:
        log("  取出压缩包内文件失败：%r" % e)
        return ""
    return dst


def _prepare_nested(path, app=None, log=print):
    """下载完就判断：这个 zip 是不是"外壳 + 官方安装程序"？

    是的话直接取出安装程序并返回它的路径，这样后面 静默安装 / 依次安装 /
    管理员批量安装 三条路拿到的都是真正的安装程序，不用各自再拆一遍包。
    """
    if not is_zip(path):
        return ""
    rel = _zip_nested_installer(path, app)
    if not rel:
        return ""
    log("这个包是 ZIP 封装，里面自带官方安装程序：%s" % rel)
    return _extract_member(path, rel, log)


def _zip_main_exe(archive):
    """ZIP 里体积最大的那个 exe——基本就是这个程序的主程序（ripgrep → rg.exe）。"""
    try:
        with zipfile.ZipFile(archive) as z:
            best = ""
            best_size = 0
            for info in z.infolist():
                n = info.filename
                base = os.path.basename(n).lower()
                if not base.endswith(".exe"):
                    continue
                if any(h in base for h in ("unins", "setup", "install", "update", "crash")):
                    continue
                if info.file_size > best_size:
                    best, best_size = os.path.basename(n), info.file_size
            return best
    except Exception:
        return ""


def _find_zip_target(app, exe_name, log=print):
    """找到这个绿色版真正的安装目录：必须真存在，且里面有主程序。

    优先级：app 里记的目录 → 注册表 InstallLocation → winget 的 Packages 目录。
    winget 的 ZIP 包会放在 `<Packages>\\<id>_xxx\\<版本目录>\\` 下，所以还要往里
    找一层——找到"主程序真正在的那层"，覆盖才不会装错地方。
    """
    cands = []
    for k in ("target", "install_location", "location"):
        v = (app.get(k) or "").strip().strip('"')
        if v:
            cands.append(v)
    if app.get("exe"):
        cands.append(os.path.dirname(app["exe"]))
    loc = _registry_location(app.get("name") or "")
    if loc:
        cands.append(loc)
    base = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                        "Microsoft", "WinGet", "Packages")
    pid = (app.get("id") or "").strip()
    if pid and os.path.isdir(base):
        try:
            for d in sorted(os.listdir(base)):
                if d.lower().startswith(pid.lower()):
                    cands.append(os.path.join(base, d))
        except OSError:
            pass

    seen = set()
    for c in cands:
        try:
            c = os.path.abspath(c)
        except OSError:
            continue
        if c in seen or not os.path.isdir(c):
            continue
        seen.add(c)
        if not exe_name:
            return c
        if os.path.isfile(os.path.join(c, exe_name)):
            return c
        for cur, dirs, files in os.walk(c):
            if exe_name.lower() in [f.lower() for f in files]:
                return cur
    return ""


def _winget_repair(app, log=print, progress=None, timeout=1200, exe_name=""):
    """没找到已安装目录时，让 winget 自己重装（ZIP 包的正规安装方式）。"""
    pid = (app.get("id") or "").strip()
    name = app.get("name") or pid or ""
    if not pid:
        return _result(False, "failed",
                       "找不到已安装目录，也没有 winget id，无法自动修复；"
                       "请手动把压缩包解压到目标目录")
    before = _registry_version(app.get("name") or pid)
    base = ["--id", pid, "-e", "--accept-package-agreements",
            "--accept-source-agreements", "--disable-interactivity"]
    attempts = (["winget", "upgrade"] + base,
                ["winget", "install"] + base + ["--force"])
    text = ""
    for cmd in attempts:
        log("用 winget 重新安装: %s" % " ".join(cmd[:3]))
        rc, out, err = _run_capture(cmd, timeout)
        text = ((out or "") + "\n" + (err or "")).strip()
        if rc == 0:
            after = _registry_version(app.get("name") or pid)
            if before and after and before != after:
                log("  版本: %s -> %s" % (before, after))
            elif after:
                log("  当前版本: %s" % after)
            # winget 会把文件放进新的版本子目录 → 顺手把 PATH 里的旧路径纠正过来
            if exe_name:
                newdir = _find_zip_target(app, exe_name, log)
                if newdir:
                    log("  主程序位置: %s" % newdir)
                    _fix_stale_path(app, newdir, exe_name, log)
            _emit(progress, phase="install", app=name, percent=100.0)
            return _result(True, "success", "")
        log("  这次没成功（退出码 %s），换另一种方式" % rc)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("-", "\\", "|")):
            log("  " + line[:200])
    res = classify_install(rc, text)
    if not res.get("ok"):
        res["reason"] = ("winget 重装也失败了：%s" % (res.get("reason") or "")).strip("：")
    return res


def _broadcast_env_change():
    """告诉资源管理器环境变量变了（新开的窗口才能立刻用上新 PATH）。"""
    try:
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        fn = ctypes.windll.user32.SendMessageTimeoutW
        fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                       ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_ulong)]
        res = ctypes.c_ulong()
        fn(ctypes.c_void_p(HWND_BROADCAST), WM_SETTINGCHANGE, ctypes.c_void_p(0),
           "Environment", SMTO_ABORTIFHUNG, 5000, ctypes.byref(res))
    except Exception:
        pass


def _read_user_path():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
        try:
            val, typ = winreg.QueryValueEx(key, "Path")
        finally:
            key.Close()
        return str(val or ""), typ
    except OSError:
        return "", None


def _write_user_path(value, typ):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                         winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, "Path", 0, typ or winreg.REG_EXPAND_SZ, value)
    finally:
        key.Close()
    _broadcast_env_change()


def _fix_stale_path(app, new_dir, exe_name, log=print):
    """把 PATH 里指向"旧版本已不存在目录"的条目换成新目录。

    真实案例：ripgrep 的 PATH 写死了
      ...\\BurntSushi.ripgrep.MSVC_xxx\\ripgrep-15.1.0-x86_64-pc-windows-msvc
    升级后文件进了 15.2.0 那个目录，旧目录已不存在 → `rg` 依然"找不到命令"，
    用户看到的就是"装了但用不了 / 安装不了"。
    只动"同一个程序、且目录已失效"的条目，改之前把原 PATH 存一份备份。
    """
    if not new_dir or not os.path.isdir(new_dir):
        return ""
    value, typ = _read_user_path()
    if not value:
        return ""
    pid = (app.get("id") or "").lower()
    new_base = os.path.basename(new_dir.rstrip("\\/")).lower()
    strip_ver = lambda s: re.sub(r"\d+(\.\d+)*", "", s)

    entries = value.split(";")
    changed, notes = [], []
    for i, e in enumerate(entries):
        raw = e.strip().strip('"')
        if not raw:
            continue
        d = os.path.expandvars(raw)
        if os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(new_dir)):
            continue
        base = os.path.basename(raw.rstrip("\\/")).lower()
        same_app = bool(pid) and pid in raw.lower()
        if not same_app and base and new_base:
            a, b = strip_ver(base), strip_ver(new_base)
            same_app = a == b and base != new_base
        if not same_app:
            continue
        alive = os.path.isdir(d) and (
            not exe_name or os.path.isfile(os.path.join(d, exe_name)))
        if alive:
            continue
        entries[i] = new_dir
        changed.append((raw, new_dir))

    if not changed:
        return ""
    try:
        os.makedirs(os.path.join(BACKUP_DIR, "_path"), exist_ok=True)
        bak = os.path.join(BACKUP_DIR, "_path",
                           "PATH-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        with open(bak, "w", encoding="utf-8") as f:
            f.write(value)
        log("  PATH 原值已备份: %s" % bak)
    except OSError:
        pass
    _write_user_path(";".join(entries), typ)
    for old, new in changed:
        log("  已修正 PATH: %s → %s" % (old, new))
    log("  （新开的命令行窗口才会生效，已打开的窗口不认）")
    return ";".join(entries)


def _install_nested_zip(path, app, log=print, progress=None, cancel=None,
                        interactive=False):
    """ZIP 只是外壳，真正要跑的是包里那个厂商安装程序。"""
    name = (app or {}).get("name") or ""
    rel = _zip_nested_installer(path, app)
    if not rel:
        return _result(False, "failed", "压缩包里没有找到安装程序")
    _emit(progress, phase="install", app=name, percent=None)
    log("这个 ZIP 是外壳，真正的安装程序在包里：%s" % rel)
    inner = _extract_member(path, rel, log)
    if not inner or not os.path.isfile(inner):
        return _result(False, "failed",
                       "没能从压缩包里取出安装程序（压缩包可能损坏）")
    info = detect_installer(inner)
    log("  安装程序框架: %s" % info["label"])
    pkg2 = {"path": inner, "itype": ""}
    if interactive:
        return install_interactive(app, pkg2, log=log, progress=progress,
                                   cancel=cancel)
    return _install_winget(inner, "", app, log=log, progress=progress,
                           cancel=cancel)


def install_zip_app(path, app, log=print, progress=None, cancel=None):
    """ZIP 绿色版包：解压覆盖，绝不执行。

    真实案例：winget 的 `BurntSushi.ripgrep.MSVC` 是 zip 安装器，下载下来的是
    `ripgrep-15.2.0-x86_64-pc-windows-msvc.zip`。旧逻辑把它当 exe 执行，
    直接抛 `[WinError 193] 不是有效的 Win32 应用程序` → 列表上就是"安装不了"。
    这类包的安装方式只有一种：把包里的文件覆盖到原安装目录。
    """
    name = (app or {}).get("name") or ""
    if not path or not os.path.exists(path):
        return _result(False, "failed", "安装包不存在：%s" % path)
    if not zipfile.is_zipfile(path):
        return _result(False, "failed",
                       "不是有效的 ZIP 压缩包（可能下载不完整）：%s"
                       % os.path.basename(path))
    if _zip_nested_installer(path, app):
        # 不是绿色版：里面躺着厂商安装程序（如 Epic Online Services）
        return _install_nested_zip(path, app, log=log, progress=progress,
                                   cancel=cancel)
    _emit(progress, phase="install", app=name, percent=None)
    log("ZIP 绿色版包 → 解压覆盖（这类包没有安装程序，不能双击/执行）")

    exe_name = _zip_main_exe(path)
    if exe_name:
        log("压缩包主程序: %s" % exe_name)
    target = _find_zip_target(app, exe_name, log)
    if not target:
        log("没找到已安装目录（注册表那条记录可能是残留）→ 改用 winget 重装")
        return _winget_repair(app, log=log, progress=progress, exe_name=exe_name)

    before = ""
    exe_path = os.path.join(target, exe_name) if exe_name else ""
    if exe_path and os.path.isfile(exe_path):
        before = _exe_version(exe_path)
        if before:
            log("当前版本: %s" % before)
    log("安装目录: %s" % target)
    if cancel and cancel():
        return _result(False, "cancelled", "已取消")

    try:
        applied = _apply(path, target, app, log=log, progress=progress)
    except OSError as e:
        return _classify_oserr(e)
    if not applied:
        return _result(False, "failed",
                       "解压覆盖失败：原程序可能正在运行，或被安全软件拦住")
    if exe_name and not os.path.isfile(exe_path):
        return _result(False, "failed",
                       "解压后没找到主程序 %s，目标目录可能不对：%s"
                       % (exe_name, target))

    after = _exe_version(exe_path) if exe_name else ""
    if before and after and before != after:
        log("  版本: %s -> %s" % (before, after))
    _fix_stale_path(app, target, exe_name, log)
    _emit(progress, phase="install", app=name, percent=100.0)
    res = _result(True, "success", "")
    if before and after and before == after:
        res["reason"] = "文件已覆盖，但主程序版本号仍是 %s，请确认是否真的更新" % after
    return res


def update_portable(app, log=print, progress=None, cancel=None):
    repo = (app.get("id") or "").strip()
    name = app.get("name") or ""
    if not repo:
        log("该程序未配置更新源（github），跳过")
        return _result(False, "failed", "未配置更新源（github），跳过")

    _emit(progress, phase="query", app=name, percent=None)
    log("查询 %s 最新发布..." % repo)
    try:
        rel = gh.latest_release(repo)
    except Exception as e:
        log("查询失败: %r" % e)
        return _result(False, "failed", "查询最新版本失败：%s" % e)

    asset = gh.pick_asset(rel, app.get("asset") or "*")
    if not asset:
        log("该发布没有可下载文件")
        return _result(False, "failed", "该发布没有可下载文件")

    size = asset.get("size") or 0
    dest = os.path.join(CACHE_DIR, _safe(asset.get("name") or "download.bin"))
    log("下载 %s (%.1f MB)" % (asset.get("name"), size / 1048576.0))
    try:
        dl = download(asset["browser_download_url"], dest, log=log,
                      progress=progress, app_name=name, cancel=cancel)
    except Exception as e:
        log("下载失败: %r" % e)
        return _result(False, "failed", "下载失败：%s" % e)
    if not dl:
        if cancel and cancel():
            return _result(False, "cancelled", "用户取消")
        return _result(False, "failed", "下载失败")

    target = app.get("target") or (os.path.dirname(app["exe"]) if app.get("exe") else "")
    if not target or not os.path.isdir(target):
        log("目标目录无效: %r" % target)
        return _result(False, "failed", "目标目录无效：%s" % target)
    try:
        applied = _apply(dest, target, app, log=log, progress=progress)
    except Exception as e:
        return _result(False, "failed", "覆盖出错：%s" % e)
    if applied:
        return _result(True, "success", "")
    return _result(False, "failed", "覆盖未完成（文件可能被占用）")


def _apply(archive, target, app, log=print, progress=None):
    name = app.get("name") or ""
    backup_root = os.path.join(BACKUP_DIR, _safe(name), time.strftime("%Y%m%d-%H%M%S"))
    tmp = tempfile.mkdtemp(prefix="oku-")
    try:
        if zipfile.is_zipfile(archive):
            _emit(progress, phase="extract", app=name, percent=None)
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                total = len(names) or 1
                for k, n in enumerate(names, 1):
                    z.extract(n, tmp)
                    if k % 5 == 0 or k == total:
                        _emit(progress, phase="extract", app=name, done=k,
                              total=total, percent=k * 100.0 / total)
            src = tmp
            entries = os.listdir(tmp)
            if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0])):
                src = os.path.join(tmp, entries[0])
        else:
            dst = os.path.join(target, os.path.basename(archive))
            if os.path.exists(dst):
                os.makedirs(backup_root, exist_ok=True)
                shutil.copy2(dst, os.path.join(backup_root, os.path.basename(archive)))
            _emit(progress, phase="apply", app=name, percent=None)
            shutil.copy2(archive, dst)
            log("非压缩包，已覆盖: " + dst)
            return True

        sub = (app.get("from") or "").strip("\\/")
        if sub:
            src = os.path.join(src, *sub.split("/"))
        if not os.path.isdir(src):
            log("压缩包内路径不存在: %r" % sub)
            return False

        files = []
        for cur, _dirs, fnames in os.walk(src):
            for f in fnames:
                files.append((cur, f))
        total = len(files) or 1
        for k, (cur, f) in enumerate(files, 1):
            rel = os.path.relpath(cur, src)
            dst_dir = target if rel == "." else os.path.join(target, rel)
            os.makedirs(dst_dir, exist_ok=True)
            s = os.path.join(cur, f)
            d = os.path.join(dst_dir, f)
            if os.path.exists(d):
                b = os.path.join(backup_root, "" if rel == "." else rel, f)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                try:
                    shutil.copy2(d, b)
                except OSError:
                    pass
            shutil.copy2(s, d)
            if k % 10 == 0 or k == total:
                _emit(progress, phase="apply", app=name, done=k,
                      total=total, percent=k * 100.0 / total)
        log("覆盖完成: %d 个文件" % total)
        log("原文件已备份到: " + backup_root)
        _emit(progress, phase="apply", app=name, done=total,
              total=total, percent=100.0)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
