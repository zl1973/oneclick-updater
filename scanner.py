"""本机程序扫描：注册表已安装程序 + 绿色/便携版目录 + 结果合并。"""
import ctypes
import difflib
import os
import re
import winreg

import gh

SKIP_NAME_HINTS = (
    "uninstall", "unins", "setup", "installer", "update", "updater",
    "crash", "report", "helper", "vc_redist", "vcredist", "dxsetup",
    "elevate", "repair", "register",
)

LANGS = ("040904b0", "080404b0", "040704b0", "000004b0", "040904e4", "040704e4")

_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
)


def get_version_info(path):
    """读取 exe 的版本资源，失败返回 {}。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return {}
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return {}
        base = ctypes.cast(buf, ctypes.c_void_p)
        info = {}
        for key in ("ProductName", "FileDescription", "FileVersion", "ProductVersion", "CompanyName"):
            for lang in LANGS:
                ptr = ctypes.c_void_p()
                length = ctypes.c_uint()
                sub = "\\StringFileInfo\\%s\\%s" % (lang, key)
                try:
                    ok = ctypes.windll.version.VerQueryValueW(
                        base, sub, ctypes.byref(ptr), ctypes.byref(length)
                    )
                except Exception:
                    ok = 0
                if ok and ptr.value:
                    try:
                        val = ctypes.wstring_at(ptr.value, length.value)
                    except Exception:
                        val = ""
                    val = val.replace("\x00", "").strip()
                    if val:
                        info[key] = val
                        break
        return info
    except Exception:
        return {}


def _norm(s):
    s = (s or "").lower()
    s = re.sub(r"[\u00ae\u2122\u00a9]", "", s)
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\d+(\.\d+)*", " ", s)
    s = re.sub(r"[\s._\-]+", "", s)
    return s


def _guess_main_exe(location, name):
    if not location or not os.path.isdir(location):
        return ""
    cands = []
    try:
        for f in os.listdir(location):
            if not f.lower().endswith(".exe"):
                continue
            low = f.lower()
            if any(h in low for h in SKIP_NAME_HINTS):
                continue
            p = os.path.join(location, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            score = difflib.SequenceMatcher(None, _norm(name), _norm(f)).ratio()
            cands.append((score, size, p))
    except OSError:
        return ""
    if not cands:
        return ""
    cands.sort(reverse=True)
    return cands[0][2]


def scan_registry():
    """扫描卸载注册表，返回程序列表。"""
    apps = {}
    for root, path, flag in _KEYS:
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
                sub_name = winreg.EnumKey(key, i)
            except OSError:
                continue
            try:
                sk = winreg.OpenKey(key, sub_name)
            except OSError:
                continue
            try:
                data = {}
                for j in range(winreg.QueryInfoKey(sk)[1]):
                    try:
                        vname, value, _ = winreg.EnumValue(sk, j)
                    except OSError:
                        continue
                    data[vname] = value
                name = str(data.get("DisplayName") or "").strip()
                if not name:
                    continue
                if data.get("SystemComponent") in (1, "1"):
                    continue
                if data.get("ParentKeyName") or data.get("ReleaseType") in (
                    "Update", "Hotfix", "Security Update", "ServicePack"
                ):
                    continue
                publisher = str(data.get("Publisher") or "").strip()
                key_name = name + "|" + publisher
                if key_name in apps:
                    continue
                location = str(data.get("InstallLocation") or "").strip().strip('"')
                version = str(data.get("DisplayVersion") or data.get("Version") or "").strip()
                apps[key_name] = {
                    "name": name,
                    "version": version,
                    "publisher": publisher,
                    "location": location,
                    "uninstall": str(data.get("UninstallString") or ""),
                    "exe": _guess_main_exe(location, name),
                }
            finally:
                sk.Close()
        key.Close()
    return list(apps.values())


def scan_portable(dirs, max_depth=3):
    """扫描绿色软件目录，每个文件夹只保留体积最大的那个 exe。"""
    found = []
    for root_dir in dirs or []:
        if not root_dir or not os.path.isdir(root_dir):
            continue
        root_dir = os.path.abspath(root_dir)
        base_depth = root_dir.rstrip("\\/").count(os.sep)
        for cur, dnames, fnames in os.walk(root_dir):
            depth = cur.rstrip("\\/").count(os.sep) - base_depth
            if depth >= max_depth:
                dnames[:] = []
            best = None
            for f in fnames:
                if not f.lower().endswith(".exe"):
                    continue
                low = f.lower()
                if any(h in low for h in SKIP_NAME_HINTS):
                    continue
                p = os.path.join(cur, f)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                if size < 512 * 1024:
                    continue
                if best is None or size > best[0]:
                    best = (size, p)
            if not best:
                continue
            p = best[1]
            vi = get_version_info(p)
            name = (vi.get("ProductName") or vi.get("FileDescription") or "").strip()
            if not name or len(name) > 60:
                name = os.path.basename(cur)
            version = (vi.get("ProductVersion") or vi.get("FileVersion") or "").strip()
            found.append({
                "name": name,
                "version": version,
                "exe": p,
                "company": (vi.get("CompanyName") or "").strip(),
            })
    return found


def _index_registry(registry):
    return {_norm(a["name"]): a for a in registry}


def _match_exe(name, index):
    if not index:
        return ""
    key = _norm(name)
    if key in index:
        return index[key].get("exe") or ""
    best, score = None, 0.0
    for k, v in index.items():
        r = difflib.SequenceMatcher(None, key, k).ratio()
        if r > score:
            best, score = v, r
    if score >= 0.86:
        return best.get("exe") or ""
    return ""


def build_apps(winget_rows, registry, cfg, portable_found, log=print):
    apps = []
    index = _index_registry(registry)

    for row in winget_rows:
        pid = (row.get("id") or "").strip()
        if not pid:
            continue
        apps.append({
            "name": row.get("name") or pid,
            "id": pid,
            "current": row.get("current") or "",
            "latest": row.get("latest") or "",
            "kind": "winget",
            "source": row.get("source") or "winget",
            "exe": _match_exe(row.get("name") or "", index),
            "target": "",
            "notes": "",
        })

    configured = []
    for item in cfg.get("portable_apps") or []:
        exe = (item.get("exe") or "").strip()
        repo = (item.get("github") or "").strip()
        current = ""
        if exe and os.path.isfile(exe):
            vi = get_version_info(exe)
            current = (vi.get("ProductVersion") or vi.get("FileVersion") or "").strip()
        latest = ""
        if repo:
            log("   查询 %s 的最新版本..." % (item.get("name") or repo))
            latest = gh.latest_tag(repo)
        configured.append({
            "name": item.get("name") or os.path.splitext(os.path.basename(exe))[0],
            "id": repo,
            "current": current,
            "latest": latest,
            "kind": "portable" if repo else "portable-nosource",
            "source": ("github: " + repo) if repo else "",
            "exe": exe,
            "target": (item.get("target") or (os.path.dirname(exe) if exe else "")),
            "asset": item.get("asset") or "*",
            "from": item.get("from") or "",
            "notes": "",
        })
    apps.extend(configured)

    known = set()
    for a in configured:
        if a.get("exe"):
            known.add(os.path.normcase(os.path.abspath(a["exe"])))
    for p in portable_found or []:
        path = os.path.normcase(os.path.abspath(p["exe"]))
        if path in known:
            continue
        apps.append({
            "name": p["name"],
            "id": "",
            "current": p["version"],
            "latest": "",
            "kind": "portable-nosource",
            "source": "",
            "exe": p["exe"],
            "target": os.path.dirname(p["exe"]),
            "notes": "",
        })

    names = {_norm(a["name"]) for a in apps}
    for r in registry:
        if _norm(r["name"]) in names:
            continue
        apps.append({
            "name": r["name"],
            "id": "",
            "current": r["version"],
            "latest": "",
            "kind": "other",
            "source": "",
            "exe": r.get("exe") or "",
            "target": os.path.dirname(r["exe"]) if r.get("exe") else "",
            "notes": "",
        })
    return apps
