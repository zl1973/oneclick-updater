"""一键更新 —— 主界面（v0.16，只显示待更新项 + 序号列 + 拖动调行间距/列宽 + 不更新按钮 + 保留下载开关）

流程：扫描本机程序 -> 对比线上最新版 -> 只列出「需要更新」的项 -> 勾选后
      「下载更新」(多连接显示每秒速度+进度) -> 「安装更新」(点击才装，
      权限不足时自动弹出管理员安装确认)。装好的行刷新后自动消失。
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import scanner
import updater
import version as ver
import winget_source

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")

CHECKED = "\u2611"
UNCHECKED = "\u2610"

KIND_LABEL = {
    "winget": "包管理器",
    "portable": "绿色版(有源)",
    "portable-nosource": "绿色版(无源)",
    "other": "未纳管",
}

# 升级结果分类 -> 列表里显示的短标签
RESULT_SHORT = {
    "success": "成功",
    "not_applicable": "不适用本机",
    "permission": "权限/占用",
    "manual": "需确认",
    "failed": "失败",
    "cancelled": "已取消",
}
# 结果分类 -> 行颜色标签
RESULT_TAG = {
    "success": "ok",
    "not_applicable": "na",
    "permission": "fail",
    "manual": "warn",
    "failed": "fail",
    "cancelled": "pend",
}


def load_config():
    cfg = {"winget_timeout": 300, "portable_scan_dirs": [], "portable_apps": []}
    try:
        with open(CONFIG, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    cfg["portable_scan_dirs"] = [d for d in (cfg.get("portable_scan_dirs") or []) if d]
    cfg["portable_apps"] = cfg.get("portable_apps") or []
    return cfg


def save_config(cfg):
    """把界面选项写回 config.json（保留原有其它字段）。"""
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.apps = []
        self.fetched = {}
        self.checked = set()
        self.busy = False
        self.prog_indeterminate = False
        self.cancel_event = threading.Event()
        self.q = queue.Queue()
        self.results = {}        # index -> (status, reason)  本次结果
        self.last_results = {}    # app 标识 -> (status, reason)  跨扫描保留
        self.downloaded = {}      # index -> pkg(dict)  已下载待安装
        self.visible = []         # 当前列表里显示的行（原 apps 下标）
        # 界面选项（持久化到 config.json）
        self.keep_download = bool(self.cfg.get("keep_download", True))
        self.only_updatable = bool(self.cfg.get("only_updatable", True))
        self.row_height = max(16, min(72, int(self.cfg.get("row_height") or 26)))
        # 手动标记「不更新」的程序（按 id / exe / 名称记，存 config.json）
        self.ignored = [str(x) for x in (self.cfg.get("ignored") or []) if str(x).strip()]
        self.ignored_lc = {x.lower() for x in self.ignored}
        if ("keep_download" not in self.cfg or "only_updatable" not in self.cfg
                or "row_height" not in self.cfg):
            self.cfg["keep_download"] = self.keep_download
            self.cfg["only_updatable"] = self.only_updatable
            self.cfg["row_height"] = self.row_height
            save_config(self.cfg)

        root.title("一键更新 v0.16")
        root.geometry("1180x760")
        root.minsize(900, 560)

        self._build_ui()
        self._pump()
        root.after(350, self.scan)

    # ---------------- UI ----------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        row1 = ttk.Frame(top)
        row1.pack(fill="x")
        self.btn_refresh = ttk.Button(row1, text="刷新", command=self.refresh_list,
                                      width=6)
        self.btn_refresh.pack(side="left")
        self.btn_scan = ttk.Button(row1, text="重新扫描", command=self.scan)
        self.btn_scan.pack(side="left", padx=(6, 0))
        self.btn_notes = ttk.Button(row1, text="查看更新说明", command=self.show_notes)
        self.btn_notes.pack(side="left", padx=(6, 0))

        self.btn_select = ttk.Menubutton(row1, text="选择 ▾")
        self.m_select = tk.Menu(self.btn_select, tearoff=0)
        self.m_select.add_command(label="全选", command=lambda: self.select_all("all"))
        self.m_select.add_command(label="取消全选", command=lambda: self.select_all("none"))
        self.m_select.add_command(label="仅选可更新", command=lambda: self.select_all("updatable"))
        self.m_select.add_command(label="反选", command=lambda: self.select_all("invert"))
        self.m_select.add_separator()
        self.m_select.add_command(label="已忽略（不更新）的程序…",
                                  command=self.manage_ignored)
        self.m_select.add_command(label="恢复默认列宽", command=self.reset_col_widths)
        self.btn_select.configure(menu=self.m_select)
        self.btn_select.pack(side="left", padx=(6, 0))

        self.btn_skip = ttk.Button(row1, text="不更新", command=self.mark_not_update)
        self.btn_skip.pack(side="left", padx=(6, 0))

        self.btn_dl = ttk.Button(row1, text="下载更新", command=self.download_action)
        self.btn_dl.pack(side="left", padx=(10, 0))
        self.btn_install = ttk.Menubutton(row1, text="安装更新 ▾", state="disabled")
        self.m_install = tk.Menu(self.btn_install, tearoff=0)
        self.m_install.add_command(label="依次打开安装包安装（推荐）",
                                   command=self.install_action)
        self.m_install.add_command(label="尝试静默安装（实验）",
                                   command=self.install_silent_action)
        self.btn_install.configure(menu=self.m_install)
        self.btn_install.pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(row1, text="取消", command=self.cancel_update,
                                    state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.btn_cfg = ttk.Button(row1, text="打开配置", command=self.open_config)
        self.btn_cfg.pack(side="left", padx=(6, 0))
        self.btn_admin = ttk.Button(row1, text="以管理员重启", command=self.restart_admin,
                                    state="disabled")
        self.btn_admin.pack(side="left", padx=(6, 0))

        self.status = tk.StringVar(value="准备就绪")
        ttk.Label(row1, textvariable=self.status).pack(side="right")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="进度:", width=5).pack(side="left")
        self.prog = ttk.Progressbar(row2, mode="determinate", maximum=100, value=0)
        self.prog.pack(side="left", fill="x", expand=True, padx=(4, 6))
        self.prog_text = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.prog_text, anchor="w", width=54).pack(side="left")

        row3 = ttk.Frame(top)
        row3.pack(fill="x", pady=(6, 0))
        self.var_rowh = tk.StringVar(value=str(self.row_height))

        self.var_keep = tk.BooleanVar(value=self.keep_download)
        ttk.Checkbutton(row3, text="保留下载的安装包", variable=self.var_keep,
                        command=self._on_keep_toggle).pack(side="left")
        ttk.Label(row3, text="（不勾选＝更新成功后自动删掉安装包）",
                  foreground="#8c8c8c").pack(side="left", padx=(2, 14))

        self.var_only = tk.BooleanVar(value=self.only_updatable)
        ttk.Checkbutton(row3, text="只显示需要更新的软件", variable=self.var_only,
                        command=self._on_only_toggle).pack(side="left")
        ttk.Label(row3, text="调行距＝拖上面那条灰带 ｜ 调列宽＝拖表头两列之间的竖线（双击＝自动收紧）",
                  foreground="#8c8c8c").pack(side="right")

        # 行间距：鼠标上下拖动这条灰带（不用输入框）
        self.grip = tk.Frame(top, height=13, bg="#dcdcdc", cursor="sb_v_double_arrow")
        self.grip.pack(fill="x", pady=(6, 0))
        self.grip.pack_propagate(False)
        self.grip_text = tk.Label(self.grip, text="↕  按住拖动调整行间距",
                                  bg="#dcdcdc", fg="#707070",
                                  font=("Microsoft YaHei UI", 7),
                                  cursor="sb_v_double_arrow")
        self.grip_text.pack(expand=True)
        for w in (self.grip, self.grip_text):
            w.bind("<Button-1>", self._grip_press)
            w.bind("<B1-Motion>", self._grip_drag)
            w.bind("<ButtonRelease-1>", self._grip_release)
            w.bind("<Enter>", lambda e: self.grip.configure(bg="#b9d5f0"))
            w.bind("<Leave>", lambda e: self.grip.configure(bg="#dcdcdc"))

        body = ttk.PanedWindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=10, pady=4)

        top_part = ttk.Frame(body)
        cols = ("no", "check", "name", "cur", "latest", "kind", "source", "status")
        self.tree = ttk.Treeview(top_part, columns=cols, show="headings", selectmode="browse")
        # 列定义：标题 / 默认宽 / 最小宽
        self._col_order = list(cols)
        self._col_defs = {
            "no": ("序号", 52, 40),
            "check": ("", 38, 34),
            "name": ("程序名称", 280, 130),
            "cur": ("当前版本", 118, 70),
            "latest": ("最新版本", 118, 70),
            "kind": ("类型", 88, 56),
            "source": ("更新来源", 168, 80),
            "status": ("状态", 236, 90),
        }
        saved_w = self.cfg.get("col_widths") or {}
        # 用户手动拖过的列（不再随窗口自动伸缩）
        self.manual_cols = {str(c) for c in (self.cfg.get("col_manual") or [])
                            if str(c) in self._col_defs}
        for c in cols:
            text, width, minw = self._col_defs[c]
            self.tree.heading(c, text=text)
            try:
                w = int(saved_w.get(c) or width)
            except (TypeError, ValueError):
                w = width
            # 全部 stretch=False：列宽只由「用户拖动」或「窗口宽度变化」决定，
            # 否则 Tk 会按 stretch 列重新分配，拖完立刻被还原（就是"拖不动"的原因）
            self.tree.column(c, width=max(minw, w), minwidth=minw,
                             anchor="w", stretch=False)
        self.tree.tag_configure("updatable", foreground="#ff7a45")
        self.tree.tag_configure("unknown", foreground="#8c8c8c")
        self.tree.tag_configure("ok", foreground="#49aa19")
        self.tree.tag_configure("fail", foreground="#ff4d4f")
        self.tree.tag_configure("na", foreground="#8c8c8c")
        self.tree.tag_configure("pend", foreground="#faad14")
        self.tree.tag_configure("warn", foreground="#d48806")
        # 列宽：按住表头两列之间的竖线左右拖动（命中区 6px，不用抠那 1 像素）
        self.tree.bind("<Button-1>", self._on_tree_press)
        self.tree.bind("<B1-Motion>", self._on_tree_drag)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release)
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Configure>", self._on_tree_configure)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._preview_notes())
        self.tree.bind("<Control-MouseWheel>", self._on_rowh_wheel)

        vs = ttk.Scrollbar(top_part, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(top_part, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        top_part.rowconfigure(0, weight=1)
        top_part.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        body.add(top_part, weight=3)

        bottom = ttk.Frame(body)
        ttk.Label(bottom, text="更新说明 / 运行日志").pack(anchor="w")
        txt_frame = ttk.Frame(bottom)
        txt_frame.pack(fill="both", expand=True)
        self.notes = tk.Text(txt_frame, height=10, wrap="word", state="disabled",
                             background="#1e1e1e", foreground="#d4d4d4",
                             insertbackground="#d4d4d4", relief="flat")
        ns = ttk.Scrollbar(txt_frame, orient="vertical", command=self.notes.yview)
        self.notes.configure(yscrollcommand=ns.set)
        self.notes.pack(side="left", fill="both", expand=True)
        ns.pack(side="right", fill="y")
        body.add(bottom, weight=1)

        self._apply_row_height(save=False)

    # ---------------- 界面选项（行距 / 保留安装包 / 只显示待更新）----------------
    def _apply_row_height(self, save=True):
        try:
            h = int(float(self.var_rowh.get()))
        except Exception:
            h = self.row_height
        h = max(16, min(72, h))
        self.row_height = h
        self.var_rowh.set(str(h))
        try:
            ttk.Style().configure("Treeview", rowheight=h)
        except Exception:
            pass
        self.cfg["row_height"] = h
        if save:
            save_config(self.cfg)

    def _nudge_row_height(self, delta):
        self.var_rowh.set(str(max(16, min(72, self.row_height + delta))))
        self._apply_row_height()

    def _on_rowh_wheel(self, event):
        self._nudge_row_height(2 if event.delta > 0 else -2)
        return "break"

    # 行间距：按住灰带上下拖动
    def _grip_press(self, event):
        self._grip_y0 = event.y_root
        self._grip_h0 = self.row_height
        return "break"

    def _grip_drag(self, event):
        d = event.y_root - getattr(self, "_grip_y0", event.y_root)
        h = max(16, min(72, getattr(self, "_grip_h0", self.row_height) + int(d)))
        self.var_rowh.set(str(h))
        self._apply_row_height(save=False)
        return "break"

    def _grip_release(self, event):
        self._apply_row_height()
        try:
            self.grip.configure(bg="#dcdcdc")
        except Exception:
            pass
        self.log("\n[设置] 行间距：%d 像素（拖动灰带调整，已保存）" % self.row_height)
        return "break"

    def _on_keep_toggle(self):
        self.keep_download = bool(self.var_keep.get())
        self.cfg["keep_download"] = self.keep_download
        save_config(self.cfg)
        if self.keep_download:
            self.log("\n[设置] 保留下载的安装包：开")
        else:
            self.log("\n[设置] 保留下载的安装包：关（更新成功后会自动删除安装包）")
            self.log("  当前缓存占用 %.1f MB；已下载但还没装的包不会删，装成功才删。"
                     % (self._cache_size() / 1048576.0))

    @staticmethod
    def _cache_size():
        total = 0
        for cur, _d, files in os.walk(updater.CACHE_DIR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(cur, f))
                except OSError:
                    pass
        return total

    def _on_only_toggle(self):
        self.only_updatable = bool(self.var_only.get())
        self.cfg["only_updatable"] = self.only_updatable
        save_config(self.cfg)
        self._refresh_tree()
        self.set_status(self._summary())

    # ---------------- 日志 ----------------
    def log(self, msg):
        self.q.put(("log", str(msg)))

    def set_notes(self, msg):
        self.q.put(("notes", str(msg)))

    def set_status(self, msg):
        self.q.put(("status", str(msg)))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._write(self.notes, payload + "\n")
                elif kind == "notes":
                    self._set_text(self.notes, payload)
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "apps":
                    self.apps = payload
                    self._refresh_tree()
                elif kind == "progress":
                    self._set_progress(payload)
                elif kind == "result":
                    self._on_result(payload)
                elif kind == "status_row":
                    self._set_row_status(payload)
                elif kind == "download_done":
                    self._on_download_done(payload)
                elif kind == "admin":
                    self.btn_admin.configure(state="normal" if payload else "disabled")
                elif kind == "drop_download":
                    self.downloaded.pop(payload, None)
                    if not self.busy:
                        self.btn_install.configure(
                            state="normal" if self.downloaded else "disabled")
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _write(self, widget, text):
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.btn_refresh, self.btn_scan, self.btn_select, self.btn_dl,
                  self.btn_install, self.btn_notes):
            b.configure(state=state)
        self.btn_admin.configure(state="disabled")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.root.configure(cursor="watch" if busy else "")
        if not busy:
            self.cancel_event.clear()
            if self.prog_indeterminate:
                self.prog.stop()
                self.prog.config(mode="determinate")
                self.prog_indeterminate = False
            self.prog["value"] = 0
            self.prog_text.set("")
            # 下载完成后「安装更新」才可点
            self.btn_install.configure(
                state="normal" if self.downloaded else "disabled")

    def cancel_update(self):
        if not self.busy:
            return
        self.cancel_event.set()
        self.log("\n正在取消（当前任务结束后停止）...")

    # ---------------- 进度条 ----------------
    def _fmt_speed(self, bps):
        if not bps or bps < 1:
            return ""
        if bps >= 1048576:
            return "%.2f MB/s" % (bps / 1048576.0)
        return "%.0f KB/s" % (bps / 1024.0)

    def _fmt_eta(self, sec):
        if sec is None:
            return ""
        sec = int(sec)
        if sec < 60:
            return "%ds" % sec
        if sec < 3600:
            return "%dm%02ds" % (sec // 60, sec % 60)
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)

    def _set_progress(self, info):
        phase = {
            "query": "查询更新", "download": "下载", "extract": "解压",
            "apply": "覆盖安装", "install": "安装",
        }.get(info.get("phase"), info.get("phase") or "")
        if info.get("phase") == "install" and info.get("percent") is None:
            phase = "等待在安装界面完成"
        app = info.get("app") or ""
        pct = info.get("percent")
        if pct is None:
            if not self.prog_indeterminate:
                self.prog.config(mode="indeterminate")
                self.prog.start(25)
                self.prog_indeterminate = True
            self.prog["value"] = 0
        else:
            if self.prog_indeterminate:
                self.prog.stop()
                self.prog.config(mode="determinate")
                self.prog_indeterminate = False
            pct = min(100.0, max(0.0, pct))
            self.prog["value"] = pct
        parts = []
        if app:
            parts.append("[%s]" % app)
        if phase:
            parts.append(phase)
        if pct is not None:
            parts.append("%.0f%%" % min(100.0, max(0.0, pct)))
        done, total = info.get("done"), info.get("total")
        if done and total:
            parts.append("%.2f / %.2f MB" % (done / 1048576.0, total / 1048576.0))
        elif done:
            parts.append("%.2f MB" % (done / 1048576.0))
        sp = info.get("speed")
        if sp:
            parts.append(self._fmt_speed(sp))
        eta = info.get("eta")
        if eta:
            parts.append("剩余 " + self._fmt_eta(eta))
        self.prog_text.set("    ".join(parts))

    # ---------------- 扫描 ----------------
    def scan(self, keep_results=False):
        if self.busy:
            return
        if not keep_results:
            self.last_results = {}
        self._set_busy(True)
        self.set_status("扫描中...")
        self._write(self.notes, "\n===== 开始扫描 =====\n")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            self.log("1/3 读取可更新列表（winget）...")
            rows, raw, rc = winget_source.upgrade_list(
                timeout=int(self.cfg.get("winget_timeout") or 300))
            if rc not in (0, 1):
                self.log("  winget 返回码 %s，可能未安装或被策略限制" % rc)
                tail = [l for l in raw.strip().splitlines() if l.strip()][-3:]
                for l in tail:
                    self.log("  > " + l)
            self.log("  发现 %d 个可更新项" % len(rows))

            self.log("2/3 扫描已安装程序（注册表）...")
            registry = scanner.scan_registry()
            self.log("  发现 %d 个已安装程序" % len(registry))

            self.log("3/3 扫描绿色版目录...")
            portable = scanner.scan_portable(
                self.cfg.get("portable_scan_dirs") or [], max_depth=3)
            self.log("  发现 %d 个绿色版程序" % len(portable))

            apps = scanner.build_apps(rows, registry, self.cfg, portable, log=self.log)
            # 重新扫描会重排下标：按 app 标识把「已下载待安装」搬到新下标上，
            # 否则装的可能变成另一个程序（旧下标漂移）
            old_keys = {i: self._app_key(self.apps[i])
                        for i in self.downloaded if i < len(self.apps)}
            new_by_key = {}
            for i, a in enumerate(apps):
                k = self._app_key(a)
                if k:
                    new_by_key.setdefault(k, i)
            rebuilt = {}
            for i, k in old_keys.items():
                j = new_by_key.get(k)
                if j is not None:
                    rebuilt[j] = self.downloaded[i]
            self.downloaded = rebuilt
            self.checked = {i for i, a in enumerate(apps)
                            if self._updatable(a) and not self._is_ignored(a)}
            # 已经装好的项，即使它还在可更新列表里也不显示（只列需要更新的）
            self.downloaded = {i: p for i, p in self.downloaded.items()
                               if i < len(apps) and self._updatable(apps[i])}
            self.q.put(("apps", apps))
            n = sum(1 for a in apps
                    if self._updatable(a) and not self._is_ignored(a))
            if self.only_updatable:
                self.set_status("需要更新 %d 个" % n)
            else:
                self.set_status("共 %d 个程序，%d 个需要更新" % (len(apps), n))
            self.log("扫描完成：%d 个需要更新" % n)
        except Exception as e:
            self.log("扫描出错: %r" % e)
            self.set_status("扫描失败")
        finally:
            self.q.put(("done", None))

    # ---------------- 刷新（快速核对本机版本，不联网）----------------
    def refresh_list(self):
        """刷新 = 重新核对本机已安装版本；顺手把已经升好的行从列表里去掉。

        比「重新扫描」快得多：不查 winget、不扫绿色版目录，只读注册表 + 主程序版本。
        """
        if self.busy:
            return
        if not self.apps:
            self.scan()
            return
        self._set_busy(True)
        self.set_status("刷新中...")
        self._write(self.notes, "\n===== 刷新（核对本机版本，不联网）=====\n")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            registry = scanner.scan_registry()
            by_name = {}
            for r in registry:
                by_name.setdefault(scanner._norm(r.get("name") or ""), r)
            changed, gone = 0, 0
            for i, a in enumerate(self.apps):
                old = a.get("current") or ""
                new = ""
                try:
                    if a.get("kind") == "winget":
                        r = by_name.get(scanner._norm(a.get("name") or ""))
                        new = (r or {}).get("version") or ""
                    if not new and a.get("exe") and os.path.isfile(a["exe"]):
                        new = updater._exe_version(a["exe"])
                except Exception:
                    new = ""
                if new and new != old:
                    a["current"] = new
                    changed += 1
                    self.log("  %s: %s → %s" % (a.get("name"), old or "-", new))
                if self._updatable(a) or self._is_ignored(a):
                    if self._is_ignored(a):
                        self.checked.discard(i)
                        self.downloaded.pop(i, None)
                    continue
                gone += 1
                self.log("  %s 已是最新（%s），移出列表"
                         % (a.get("name"), a.get("current") or "-"))
                self.checked.discard(i)
                self.downloaded.pop(i, None)
            self.q.put(("apps", self.apps))
            self.log("刷新完成：版本变化 %d 个，移出 %d 个" % (changed, gone))
            self.set_status(self._summary())
        except Exception as e:
            self.log("刷新出错: %r" % e)
            self.set_status("刷新失败")
        finally:
            self.q.put(("done", None))

    def _summary(self):
        rows = [a for a in self.apps if not self._is_ignored(a)]
        n = sum(1 for a in rows if self._updatable(a))
        if self.only_updatable:
            txt = "需要更新 %d 个" % n
        else:
            txt = "共 %d 个程序，%d 个需要更新" % (len(rows), n)
        if self.ignored:
            txt += "（已忽略 %d 个）" % len(self.ignored)
        return txt

    # ---------------- 列表 ----------------
    def _updatable(self, a):
        if a.get("kind") not in ("winget", "portable"):
            return False
        if not a.get("latest"):
            return False
        return ver.is_newer(a.get("latest"), a.get("current"))

    def _visible_indices(self):
        """当前要显示的行（原 apps 下标）。默认只显示需要更新的，且跳过「不更新」的。"""
        out = []
        for i, a in enumerate(self.apps):
            if self._is_ignored(a):
                continue
            if self.only_updatable and not self._updatable(a):
                continue
            out.append(i)
        return out

    # ---------------- 不更新（忽略）----------------
    def _is_ignored(self, a):
        k = self._app_key(a)
        return bool(k) and k.lower() in self.ignored_lc

    def mark_not_update(self):
        """把勾选（没勾就取当前选中行）的程序标成「不更新」，以后不再出现在列表里。"""
        if self.busy:
            return
        targets = [i for i in sorted(self.checked) if 0 <= i < len(self.apps)]
        if not targets:
            sel = self._selected_index()
            if sel is not None and sel < len(self.apps):
                targets = [sel]
        if not targets:
            messagebox.showinfo("提示", "先勾选（或点中一行）不想更新的程序")
            return
        names = "\n".join("  · " + (self.apps[i].get("name") or "?")
                          for i in targets[:12])
        if len(targets) > 12:
            names += "\n  …共 %d 个" % len(targets)
        if not messagebox.askyesno(
                "标记为不更新",
                "这 %d 个程序以后不再出现在待更新列表里：\n\n%s\n\n"
                "随时可以在「选择 ▾ → 已忽略（不更新）的程序」里恢复。\n\n确定吗？"
                % (len(targets), names)):
            return
        for i in targets:
            a = self.apps[i]
            k = self._app_key(a)
            if k and k.lower() not in self.ignored_lc:
                self.ignored.append(k)
                self.ignored_lc.add(k.lower())
            self.checked.discard(i)
            self.downloaded.pop(i, None)
        self.cfg["ignored"] = self.ignored
        save_config(self.cfg)
        self._refresh_tree()
        self.log("\n[不更新] 已忽略：%s"
                 % "、".join(self.apps[i].get("name") or "?" for i in targets))
        self.set_status(self._summary())

    def manage_ignored(self):
        """查看 / 恢复被标成「不更新」的程序。"""
        win = tk.Toplevel(self.root)
        win.title("已忽略（不更新）的程序")
        win.geometry("520x380")
        win.transient(self.root)
        ttk.Label(win, text="这些程序不会出现在待更新列表里。选中后可恢复：",
                  padding=(10, 8)).pack(anchor="w")
        frame = ttk.Frame(win, padding=(10, 0))
        frame.pack(fill="both", expand=True)
        lb = tk.Listbox(frame, selectmode="extended")
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def fill():
            lb.delete(0, "end")
            for k in self.ignored:
                lb.insert("end", k)
            if not self.ignored:
                lb.insert("end", "（暂无）")
                lb.itemconfigure(0, foreground="#999999")
            self.lb_ignored_count = len(self.ignored)

        def restore(only_selected=True):
            idxs = list(lb.curselection()) if only_selected else list(range(len(self.ignored)))
            idxs = [i for i in idxs if 0 <= i < len(self.ignored)]
            if not idxs:
                messagebox.showinfo("提示", "先在列表里选中要恢复的程序", parent=win)
                return
            names = []
            for i in idxs:
                k = self.ignored[i]
                names.append(k)
            for i in sorted(idxs, reverse=True):
                k = self.ignored.pop(i)
                self.ignored_lc.discard(k.lower())
            self.cfg["ignored"] = self.ignored
            save_config(self.cfg)
            self._refresh_tree()
            self.log("\n[恢复更新] %s" % "、".join(names))
            self.set_status(self._summary())
            fill()

        btns = ttk.Frame(win, padding=(10, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="恢复选中", command=lambda: restore(True)).pack(side="left")
        ttk.Button(btns, text="全部恢复", command=lambda: restore(False)).pack(
            side="left", padx=(6, 0))
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="right")
        fill()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.visible = self._visible_indices()
        for n, i in enumerate(self.visible, 1):
            a = self.apps[i]
            up = self._updatable(a)
            mark = CHECKED if i in self.checked else UNCHECKED
            key = self._app_key(a)
            res = self.last_results.get(key) if key else None
            if i in self.downloaded:
                rcell = "已下载·待安装"
                tag = "pend"
            elif res:
                st, rs = res
                rlabel = RESULT_SHORT.get(st, st)
                if st in ("failed", "permission", "not_applicable") and rs:
                    rcell = "%s：%s" % (rlabel, self._trunc(rs))
                else:
                    rcell = rlabel
                tag = RESULT_TAG.get(st, "")
            else:
                rcell = "待升级" if up else ""
                tag = "updatable" if up else ("unknown" if not a.get("latest") else "")
            self.tree.insert(
                "", "end", iid=str(i), tags=(tag,) if tag else (),
                values=(n, mark, a.get("name"), a.get("current") or "-",
                        a.get("latest") or "未获取",
                        KIND_LABEL.get(a.get("kind"), a.get("kind")),
                        a.get("source") or "-",
                        rcell),
            )

    def _app_key(self, a):
        if not a:
            return None
        return a.get("id") or a.get("exe") or a.get("name")

    @staticmethod
    def _trunc(s, n=40):
        s = s or ""
        return s if len(s) <= n else s[:n - 1] + "\u2026"

    def _on_result(self, payload):
        idx, status, reason = payload
        a = self.apps[idx] if 0 <= idx < len(self.apps) else None
        key = self._app_key(a)
        if key:
            self.last_results[key] = (status, reason)
        self.results[idx] = (status, reason)
        if status == "success" and a is not None:
            # 装好了就当场从列表里消失（只显示需要更新的），不用等整轮装完再刷新
            a["current"] = a.get("latest") or a.get("current")
            self.checked.discard(idx)
            self.downloaded.pop(idx, None)
            self._refresh_tree()
            self.set_status(self._summary())
            return
        self._apply_result_row(idx, status, reason)

    def _apply_result_row(self, idx, status, reason):
        if not self.tree.exists(str(idx)):
            return
        rlabel = RESULT_SHORT.get(status, status)
        # 成功也可能带说明（例如"退出码 5，但版本已更新"），一并显示出来
        if reason and status != "cancelled":
            cell = "%s：%s" % (rlabel, self._trunc(reason))
        else:
            cell = rlabel
        self.tree.set(str(idx), "status", cell)
        tag = RESULT_TAG.get(status, "")
        self.tree.item(str(idx), tags=(tag,) if tag else ())

    def _set_row_status(self, payload):
        idx, text = payload
        if self.tree.exists(str(idx)):
            self.tree.set(str(idx), "status", text)
            if text.startswith(("下载中", "安装中", "等待")):
                self.tree.item(str(idx), tags=("pend",))

    def _on_download_done(self, ok_count):
        if ok_count and not self.busy:
            self.btn_install.configure(state="normal")
        if ok_count:
            # 下载完直接问一句要不要开始装，省得再点一次
            self.root.after(700, lambda: self._ask_install_now(ok_count))

    def _ask_install_now(self, n):
        if self.busy or not self.downloaded:
            return
        if messagebox.askyesno(
                "下载完成",
                "已下载好 %d 个安装包。\n\n现在开始依次安装吗？\n"
                "（会逐个打开安装界面，你在里面点完、关掉窗口，就自动弹下一个）"
                % n):
            self.install_action(confirm=False)

    # ---------------- 列宽：自己实现拖动（Tk 原生分隔线只有 1px，很难拖准）----------------
    def _header_height(self):
        """表头区域的高度（第一行数据的上边界）。"""
        kids = self.tree.get_children()
        if kids:
            bb = self.tree.bbox(kids[0])
            if bb:
                return bb[1]
        return 26

    def _col_edges(self):
        """每列右边界在控件内的 x 坐标（按表头文字对齐，含横向滚动偏移）。"""
        widths = [int(self.tree.column(c, "width")) for c in self._col_order]
        total = sum(widths) or 1
        try:
            off = self.tree.xview()[0] * total
        except Exception:
            off = 0.0
        edges, x = [], -off
        for c, w in zip(self._col_order, widths):
            x += w
            edges.append((x, c))
        return edges

    def _hit_edge(self, x, y, tol=6):
        """鼠标是否压在某一列的分隔线上。表头给 6px 容差，数据行 3px（不干扰选中）。"""
        if y > self._header_height() + 2:
            tol = 3
        best, bestd = None, tol + 1
        for ex, c in self._col_edges():
            d = abs(x - ex)
            if d < bestd:
                best, bestd = c, d
        return best if bestd <= tol else None

    def _on_tree_press(self, event):
        c = self._hit_edge(event.x, event.y)
        if c:
            self._drag_col = c
            self._drag_x0 = event.x
            self._drag_w0 = {k: int(self.tree.column(k, "width"))
                             for k in self._col_order}
            self._dragging_col = True
            try:
                self.tree.configure(cursor="sb_h_double_arrow")
            except Exception:
                pass
            return "break"
        self._on_click(event)

    def _on_tree_drag(self, event):
        if not getattr(self, "_dragging_col", False):
            return
        c = getattr(self, "_drag_col", "")
        if c not in self._col_order:
            return
        i = self._col_order.index(c)
        if i + 1 >= len(self._col_order):
            return "break"
        right = self._col_order[i + 1]
        dx = event.x - getattr(self, "_drag_x0", event.x)
        w0 = getattr(self, "_drag_w0", {})
        min_l = self._col_defs[c][2]
        min_r = self._col_defs[right][2]
        pair = w0.get(c, 100) + w0.get(right, 100)
        wl = w0.get(c, 100) + dx
        wr = w0.get(right, 100) - dx
        if wl < min_l:
            wl, wr = min_l, pair - min_l
        if wr < min_r:
            wr, wl = min_r, pair - min_r
        self.tree.column(c, width=int(max(min_l, wl)))
        self.tree.column(right, width=int(max(min_r, wr)))
        try:
            self.status.set("列宽：%s %d px ｜ %s %d px" % (
                self._col_defs[c][0] or "勾选", int(self.tree.column(c, "width")),
                self._col_defs[right][0] or "勾选", int(self.tree.column(right, "width"))))
        except Exception:
            pass
        return "break"

    def _on_tree_release(self, event):
        if not getattr(self, "_dragging_col", False):
            return
        self._dragging_col = False
        c = getattr(self, "_drag_col", "")
        i = self._col_order.index(c) if c in self._col_order else -1
        touched = {c} if c else set()
        if 0 <= i + 1 < len(self._col_order):
            touched.add(self._col_order[i + 1])
        # 手动拖过的列不再参与"随窗口自动伸缩"
        self.manual_cols |= touched
        self.cfg["col_manual"] = sorted(self.manual_cols)
        self._save_col_widths()
        try:
            self.tree.configure(cursor="")
        except Exception:
            pass
        self._cur_cursor = ""
        try:
            self.status.set(self._summary())
        except Exception:
            pass
        if touched:
            self.log("\n[设置] 列宽已保存：%s" % "、".join(
                "%s %dpx" % (self._col_defs[k][0] or "勾选", self.tree.column(k, "width"))
                for k in self._col_order if k in touched))
        return "break"

    def _on_tree_motion(self, event):
        if getattr(self, "_dragging_col", False):
            return "break"
        want = "sb_h_double_arrow" if self._hit_edge(event.x, event.y) else ""
        if getattr(self, "_cur_cursor", "") != want:
            try:
                self.tree.configure(cursor=want)
            except Exception:
                pass
            self._cur_cursor = want

    def _on_tree_double(self, event):
        c = self._hit_edge(event.x, event.y)
        if c:
            self._autosize_col(c)
            return "break"
        self.show_notes()

    def _autosize_col(self, col):
        """双击列分隔线：把这一列收到刚好的宽度。"""
        try:
            from tkinter import font as tkfont
            f = tkfont.nametofont("TkDefaultFont")
        except Exception:
            return
        head = self._col_defs[col][0]
        widest = f.measure(head) if head else 0
        for iid in self.tree.get_children():
            txt = str(self.tree.set(iid, col) or "")
            if txt:
                widest = max(widest, f.measure(txt))
        w = max(self._col_defs[col][2], min(560, widest + 24))
        self.tree.column(col, width=w)
        self.manual_cols.add(col)
        self.cfg["col_manual"] = sorted(self.manual_cols)
        self._save_col_widths()
        self.log("\n[设置] 列宽自适应：%s → %d px" % (head or "勾选", w))

    def _save_col_widths(self):
        self.cfg["col_widths"] = {c: int(self.tree.column(c, "width"))
                                  for c in self._col_order}
        save_config(self.cfg)

    def _on_tree_configure(self, event):
        """窗口宽度变化时，把多余的宽度补给没被手动调整过的列。"""
        if event.width == getattr(self, "_last_tree_w", -1):
            return
        self._last_tree_w = event.width
        self.root.after_idle(self._fit_columns)

    def _fit_columns(self):
        avail = self.tree.winfo_width() - 2
        if avail <= 80:
            return
        flex = [c for c in self._col_order if c not in self.manual_cols]
        if not flex:
            return
        total = sum(int(self.tree.column(c, "width")) for c in self._col_order)
        delta = avail - total
        if abs(delta) < 2:
            return
        # 变宽：先给「程序名称」吃，吃不完再分给别的列
        # 变窄：只压「程序名称」到最小宽为止，其它列宽度不动（再窄就出横向滚动条）
        if delta > 0:
            order = (["name"] if "name" in flex else []) + \
                    [c for c in flex if c != "name"]
        else:
            order = (["name"] if "name" in flex else flex[:1])
        for c in order:
            if abs(delta) < 2:
                break
            minw = self._col_defs[c][2]
            cur = int(self.tree.column(c, "width"))
            neww = cur + delta
            if neww < minw:
                neww = minw
            delta -= (neww - cur)
            self.tree.column(c, width=neww)

    def reset_col_widths(self):
        """恢复默认列宽（也恢复"随窗口自动伸缩"）。"""
        self.manual_cols = set()
        self.cfg["col_manual"] = []
        for c in self._col_order:
            text, width, minw = self._col_defs[c]
            self.tree.column(c, width=width)
        self.cfg["col_widths"] = {c: self._col_defs[c][1] for c in self._col_order}
        save_config(self.cfg)
        self._last_tree_w = -1
        self._fit_columns()
        self.log("\n[设置] 列宽已恢复默认")

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        col = self.tree.identify_column(event.x)
        if col != "#2":          # #1=序号  #2=勾选框
            return
        idx = int(row)
        if idx in self.checked:
            self.checked.discard(idx)
        else:
            self.checked.add(idx)
        self.tree.set(row, "check", CHECKED if idx in self.checked else UNCHECKED)

    def select_all(self, mode):
        vis = self._visible_indices()
        if mode == "all":
            self.checked = set(vis)
        elif mode == "none":
            self.checked = set()
        elif mode == "updatable":
            self.checked = {i for i in vis if self._updatable(self.apps[i])}
        elif mode == "invert":
            self.checked = {i for i in vis if i not in self.checked}
        self._refresh_checks()

    def _refresh_checks(self):
        self.visible = self._visible_indices()
        for n, i in enumerate(self.visible, 1):
            if self.tree.exists(str(i)):
                self.tree.set(str(i), "no", n)
                self.tree.set(str(i), "check",
                              CHECKED if i in self.checked else UNCHECKED)

    # ---------------- 更新说明 ----------------
    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _preview_notes(self):
        idx = self._selected_index()
        if idx is None or idx >= len(self.apps):
            return
        a = self.apps[idx]
        cached = self.fetched.get(a.get("id") or a.get("name"))
        if cached:
            self.set_notes(self._format_notes(a, cached))
            return
        if a.get("kind") == "winget" and not self.busy:
            threading.Thread(target=self._fetch_notes_worker, args=(idx,), daemon=True).start()
        else:
            self.set_notes(self._format_notes(a, None))

    def show_notes(self):
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("提示", "先在列表里选中一个程序")
            return
        self._preview_notes()

    def _fetch_notes_worker(self, idx):
        a = self.apps[idx]
        try:
            info = winget_source.show_notes(a["id"])
        except Exception:
            info = None
        self.fetched[a["id"]] = info
        if self._selected_index() == idx:
            self.set_notes(self._format_notes(a, info))

    def _format_notes(self, a, info):
        lines = [
            "程序: %s" % a.get("name"),
            "当前版本: %s    线上最新: %s" % (a.get("current") or "-", a.get("latest") or "未获取"),
            "更新方式: %s" % KIND_LABEL.get(a.get("kind"), "-"),
        ]
        if a.get("exe"):
            lines.append("主程序: %s" % a["exe"])
        if info:
            if info.get("version"):
                lines.append("远端版本: %s" % info["version"])
            if info.get("url"):
                lines.append("更新说明链接: %s" % info["url"])
            lines.append("")
            lines.append(info.get("notes") or "（该程序未提供更新说明）")
        elif info is None and a.get("kind") == "winget":
            lines.append("")
            lines.append("（正在获取更新说明...）")
        else:
            lines.append("")
            lines.append("（无更新源，无法获取更新说明）")
        return "\n".join(lines)

    # ---------------- 第一步：下载 ----------------
    def download_action(self, force_all=False):
        if self.busy:
            return
        if force_all:
            targets = [(i, a) for i, a in enumerate(self.apps) if self._updatable(a)]
        else:
            targets = [(i, self.apps[i]) for i in sorted(self.checked)
                       if i < len(self.apps) and self._updatable(self.apps[i])]
        if not targets:
            messagebox.showinfo("提示", "没有可下载更新的程序（请先勾选或扫描）")
            return
        self._set_busy(True)
        self.downloaded = {}
        self.results = {}
        self.last_results = {}
        self.set_status("下载中...")
        self._write(self.notes, "\n===== 开始下载 %d 个程序（显示速度/进度）=====\n"
                    % len(targets))
        threading.Thread(target=self._download_worker, args=(targets,), daemon=True).start()

    def _download_worker(self, targets):
        prog = lambda info: self.q.put(("progress", info))
        counts = {"success": 0, "failed": 0, "cancelled": 0}
        cancelled = False
        try:
            for idx, a in targets:
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                self.q.put(("status_row", (idx, "下载中")))
                self.log("\n[%s] %s (当前 %s -> 最新 %s)"
                         % (a.get("kind"), a.get("name"), a.get("current"), a.get("latest")))
                try:
                    pkg = updater.download_app(
                        a, log=self.log, progress=prog,
                        cancel=self.cancel_event.is_set)
                except Exception as e:
                    pkg = {"ok": False, "status": "failed", "reason": "异常: %r" % e}
                if pkg.get("ok"):
                    self.downloaded[idx] = pkg
                    counts["success"] += 1
                    self.q.put(("status_row", (idx, "已下载·待安装")))
                    self.log("  已下载到: %s" % pkg.get("path"))
                else:
                    st = pkg.get("status", "failed")
                    counts[st] = counts.get(st, 0) + 1
                    self.q.put(("result", (idx, st, pkg.get("reason", ""))))
                    self.log("  下载失败: %s" % pkg.get("reason", ""))
            if cancelled:
                self.log("\n===== 下载已取消 =====")
                self.log("成功下载 %d 个，其余未下载" % counts["success"])
                self.set_status("已取消：成功下载 %d 个（点「安装更新」装已下的）"
                                % counts["success"])
            else:
                self.log("\n===== 下载完成：成功 %d / 失败 %d ====="
                         % (counts["success"], counts["failed"]))
                self.set_status("下载完成：%d 个已就绪，点「安装更新」依次安装"
                                % counts["success"])
        finally:
            self.q.put(("download_done", counts["success"]))
            self.q.put(("done", None))

    # ---------------- 第二步：安装 ----------------
    def _install_targets(self):
        """已下载的待安装列表 [(idx, app, pkg)]，以及名称清单。"""
        targets = [(i, self.apps[i], self.downloaded[i]) for i in sorted(self.downloaded)]
        return targets

    def install_action(self, confirm=True):
        """默认安装方式：依次打开安装包，装完一个自动弹下一个。"""
        if self.busy:
            return
        if not self.downloaded:
            messagebox.showinfo("提示", "还没有已下载的更新，请先点「下载更新」")
            return
        targets = self._install_targets()
        names = "\n".join("  %d. %s" % (n, a["name"])
                          for n, (_, a, _) in enumerate(targets, 1))
        if len(names) > 1400:
            names = names[:1400] + "\n  ..."
        if confirm and not messagebox.askyesno(
                "依次安装",
                "将按顺序安装这 %d 个程序：\n\n%s\n\n"
                "做法：逐个打开安装包，你在界面里点完、关掉窗口后，\n"
                "会自动弹出下一个（装完用版本号核实，不会误报失败）。\n"
                "MSIX 应用（Windows 终端 / WSL 等）会先自动关掉旧版本再装，不用点。\n"
                "ZIP 绿色版（如 ripgrep）会直接解压覆盖，也没有界面。\n"
                "ZIP 里带安装程序的（如 Epic Online Services）会自动取出安装程序\n"
                "再弹给你，跟普通安装包一样点几下就好。\n\n"
                "现在开始？" % (len(targets), names)):
            return
        self._set_busy(True)
        self.results = {}
        self.set_status("安装中...")
        self._write(self.notes, "\n===== 依次安装 %d 个程序 =====\n" % len(targets))
        threading.Thread(target=self._interactive_install_worker,
                         args=(targets,), daemon=True).start()

    def _interactive_install_worker(self, targets):
        """一个装完再开下一个；每个都用版本号核实，避免"装上了却报失败"。"""
        prog = lambda info: self.q.put(("progress", info))
        counts = {"success": 0, "not_applicable": 0, "permission": 0,
                  "manual": 0, "failed": 0, "cancelled": 0}
        self.cancel_event.clear()
        total = len(targets)
        stopped = False
        try:
            for n, (idx, a, pkg) in enumerate(targets, 1):
                if self.cancel_event.is_set():
                    stopped = True
                    for j, _b, _p in targets[n - 1:]:
                        self.q.put(("status_row", (j, "未安装（已停止）")))
                    break
                self.q.put(("status_row", (idx, "安装中 %d/%d" % (n, total))))
                self.set_status("正在安装 %d/%d：%s" % (n, total, a.get("name")))
                self.log("\n==================== 【%d/%d】%s ===================="
                         % (n, total, a.get("name")))
                try:
                    res = updater.install_interactive(
                        a, pkg, log=self.log, progress=prog,
                        cancel=self.cancel_event.is_set)
                except Exception as e:
                    res = {"ok": False, "status": "failed", "reason": "异常: %r" % e}
                status = res.get("status", "failed")
                reason = res.get("reason", "")
                counts[status] = counts.get(status, 0) + 1
                self.q.put(("result", (idx, status, reason)))
                if status == "success":
                    self.log("  结果: 成功" + ("（%s）" % reason if reason else ""))
                    if not self.keep_download:
                        self._clean_pkg(idx, pkg)
                else:
                    self.log("  结果: %s（%s）" % (
                        RESULT_SHORT.get(status, status), reason))
                # 装完一个 → 稍等再弹下一个，给系统一点时间收尾
                if n < total and not self.cancel_event.is_set():
                    nxt = targets[n][1].get("name")
                    self.log("\n  下一个：%s（2 秒后自动打开，点「取消」可停下）" % nxt)
                    for _ in range(4):
                        if self.cancel_event.is_set():
                            stopped = True
                            break
                        time.sleep(0.5)

            if stopped:
                self.log("\n===== 已停止，剩余程序未安装 =====")
                self.set_status("已停止：成功 %d / 失败 %d"
                                % (counts["success"], counts["failed"]))
            else:
                self.log("\n===== 安装汇总 =====")
                self.log("成功 %d · 不适用 %d · 权限/占用 %d · 需确认 %d · 失败 %d" % (
                    counts["success"], counts["not_applicable"],
                    counts["permission"], counts["manual"], counts["failed"]))
                self.set_status("结束：成功 %d / 不适用 %d / 需确认 %d / 失败 %d" % (
                    counts["success"], counts["not_applicable"],
                    counts["manual"], counts["failed"]))
        finally:
            self.q.put(("done", None))
            self.root.after(600, lambda: self.scan(keep_results=True))

    def install_silent_action(self):
        """备用：尝试静默安装（不弹界面）。参数按安装器框架判定，装不上会自动转手动。"""
        if self.busy:
            return
        if not self.downloaded:
            messagebox.showinfo("提示", "还没有已下载的更新，请先点「下载更新」")
            return
        targets = self._install_targets()
        names = [a["name"] for _, a, _ in targets]
        if not messagebox.askyesno(
                "静默安装（实验）",
                "将对 %d 个已下载的程序尝试静默安装：\n\n%s\n\n"
                "注意：部分安装器不支持静默（没有公开参数），可能弹窗或失败；\n"
                "失败时可按提示转手动。推荐用「依次打开安装包安装」。\n\n继续？"
                % (len(names), "\n".join(names[:12])
                   + ("\n..." if len(names) > 12 else ""))):
            return
        self._set_busy(True)
        self.results = {}
        self.set_status("静默安装中...")
        self._write(self.notes, "\n===== 开始静默安装 %d 个程序 =====\n" % len(targets))
        threading.Thread(target=self._install_worker, args=(targets,), daemon=True).start()

    def _install_worker(self, targets):
        prog = lambda info: self.q.put(("progress", info))
        counts = {"success": 0, "not_applicable": 0, "permission": 0,
                  "manual": 0, "failed": 0, "cancelled": 0}
        self.cancel_event.clear()
        cancelled = False
        needs = []    # 权限不足、待管理员重装的程序
        manual = []   # 安装器要人工点、暂时没装完的程序
        try:
            for idx, a, pkg in targets:
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                self.q.put(("status_row", (idx, "安装中")))
                self.log("\n[安装] %s" % a.get("name"))
                try:
                    res = updater.install_app(
                        a, pkg, log=self.log, progress=prog,
                        cancel=self.cancel_event.is_set)
                except Exception as e:
                    res = {"ok": False, "status": "failed", "reason": "异常: %r" % e}
                status = res.get("status", "failed")
                reason = res.get("reason", "")
                counts[status] = counts.get(status, 0) + 1
                self.q.put(("result", (idx, status, reason)))
                if status == "success":
                    self.log("  结果: 成功")
                    if not self.keep_download:
                        self._clean_pkg(idx, pkg)
                else:
                    self.log("  结果: %s（%s）" % (
                        RESULT_SHORT.get(status, status), reason))
                if status == "permission":
                    needs.append((idx, a, pkg))
                elif status == "manual":
                    manual.append((idx, a, pkg))

            if needs and not cancelled:
                self._offer_admin_reinstall(needs, counts, manual)
            if manual and not cancelled:
                self._offer_manual_install(manual, counts)

            if cancelled:
                self.log("\n===== 安装已取消 =====")
                self.set_status("已取消：成功 %d / 需管理员 %d / 失败 %d"
                                % (counts["success"], counts["permission"],
                                   counts["failed"]))
            else:
                self.log("\n===== 安装汇总 =====")
                self.log("成功 %d · 不适用本机 %d · 权限/占用 %d · 需手动 %d · 失败 %d" % (
                    counts["success"], counts["not_applicable"],
                    counts["permission"], counts["manual"], counts["failed"]))
                self.set_status("结束：成功 %d / 不适用 %d / 需管理员 %d / 需手动 %d / 失败 %d" % (
                    counts["success"], counts["not_applicable"],
                    counts["permission"], counts["manual"], counts["failed"]))
        finally:
            self.q.put(("admin", counts.get("permission", 0) > 0))
            self.q.put(("done", None))
            self.root.after(600, lambda: self.scan(keep_results=True))

    # ---------------- 管理员提权重装 ----------------
    def _ask_yesno(self, title, msg):
        """在工作线程里弹模态确认框（切回主线程执行）。"""
        box = {"r": False}
        ev = threading.Event()

        def do():
            try:
                box["r"] = messagebox.askyesno(title, msg)
            except Exception:
                box["r"] = False
            ev.set()

        self.root.after(0, do)
        ev.wait()
        return box["r"]

    def _offer_admin_reinstall(self, needs, counts, manual=None):
        names = "\n".join(a.get("name") for _, a, _ in needs)
        if not self._ask_yesno(
                "需要管理员权限",
                "以下 %d 个程序因权限不足安装失败：\n\n%s\n\n"
                "是否以管理员身份重新安装？\n"
                "（只弹一次 Windows UAC 确认，会统一安装以上程序，且复用已下载的包）"
                % (len(needs), names)):
            self.log("\n[提示] 已跳过管理员安装，可点「以管理员重启」后重试。")
            return
        # 绿色版逐个以管理员覆盖（机制不同，各自一个 UAC，无法合并）
        for idx, a, pkg in [(i, x, p) for i, x, p in needs
                            if x.get("kind") != "winget"]:
            self.q.put(("status_row", (idx, "管理员安装中")))
            self.log("\n[管理员安装] %s" % a.get("name"))
            res = updater.elevated_portable(
                pkg.get("path"),
                a.get("target") or os.path.dirname(a.get("exe") or ""),
                a, log=self.log)
            status = res.get("status", "failed")
            reason = res.get("reason", "")
            counts[status] = counts.get(status, 0) + 1
            self.q.put(("result", (idx, status, reason)))
            self.log("  结果: %s（%s）" % (RESULT_SHORT.get(status, status), reason))
        # winget / 包管理器类：合并成一次 UAC 批量安装
        winget_needs = [(i, x, p) for i, x, p in needs if x.get("kind") == "winget"]
        if winget_needs:
            self._elevated_batch_install(winget_needs, counts, manual)

    def _elevated_batch_install(self, needs, counts, manual=None, timeout=900):
        """权限不足的程序：以管理员身份、一次 UAC 装完所有，且复用已下载的安装包。

        参数不再靠猜：每个包先按二进制判定安装器框架（NSIS→/S、Inno→
        /VERYSILENT…），再带着正确参数提权执行。为了让"卡在界面"不再无限挂住，
        每个安装都有 WaitForExit 超时，超时就杀进程并记 -2（需手动）。
        """
        import tempfile
        out_path = tempfile.mktemp(suffix=".txt", prefix="oku-admin-")
        ps1 = tempfile.mktemp(suffix=".ps1", prefix="oku-admin-")
        script, plan_by_idx = updater.build_elevated_install_script(
            needs, out_path, timeout)
        # 脚本里含中文路径，必须用带 BOM 的 UTF-8，否则 PowerShell 5.1 按 GBK 读会乱码
        try:
            with open(ps1, "w", encoding="utf-8-sig") as f:
                f.write(script)
            for idx, a, pkg in needs:
                plan = plan_by_idx[idx]
                self.q.put(("status_row", (idx, "管理员安装中")))
                self.log("\n[管理员安装] %s（%s，参数 %s）"
                         % (a.get("name"), plan["info"]["label"],
                            " ".join(plan["args"]) or "无"))
            self.log("  正在请求管理员权限（一次 UAC，统一安装 %d 个，复用已下载的包）..."
                     % len(needs))
            updater.run_elevated_ps1(ps1, log=self.log)
            codes = {}
            msgs = {}
            try:
                with open(out_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("RESULT|"):
                            parts = line.split("|")
                            if len(parts) == 3:
                                try:
                                    codes[int(parts[1])] = int(parts[2])
                                except ValueError:
                                    pass
                        elif line.startswith("MSG|"):
                            parts = line.split("|", 2)
                            if len(parts) == 3:
                                try:
                                    msgs[int(parts[1])] = parts[2]
                                except ValueError:
                                    pass
            except Exception as e:
                self.log("  读取管理员安装结果失败: %r" % e)
            for idx, a, pkg in needs:
                code = codes.get(idx)
                if code is None:
                    res = updater._result(False, "failed",
                                          "管理员安装未返回结果（可能 UAC 被取消）")
                else:
                    res = updater.classify_install(code, msgs.get(idx, ""))
                status = res.get("status", "failed")
                reason = res.get("reason", "")
                counts[status] = counts.get(status, 0) + 1
                self.q.put(("result", (idx, status, reason)))
                self.log("  %s 结果: %s（%s）"
                         % (a.get("name"), RESULT_SHORT.get(status, status), reason))
                if status == "manual" and manual is not None:
                    manual.append((idx, a, pkg))
        finally:
            updater._safe_remove(ps1)
            updater._safe_remove(out_path)

    def _offer_manual_install(self, items, counts):
        """有些安装器只肯弹界面、不认静默参数：帮用户把安装包依次打开，点几下就能装完。"""
        names = "\n".join(a.get("name") for _, a, _ in items)
        if not self._ask_yesno(
                "需要手动点几下",
                "以下 %d 个程序的安装器不支持全静默（会弹出安装界面）：\n\n%s\n\n"
                "要现在依次打开这些安装包吗？\n"
                "（在弹窗里点完即可，装完列表会自动刷新状态）"
                % (len(items), names)):
            self.log("\n[提示] 可稍后手动双击这些安装包：")
            for _, a, pkg in items:
                self.log("    %s -> %s" % (a.get("name"), pkg.get("path")))
            return
        for idx, a, pkg in items:
            path = pkg.get("path")
            self.log("\n[手动安装] %s" % a.get("name"))
            self.q.put(("status_row", (idx, "手动安装中")))
            updater.launch_installer_visible(path, log=self.log)
            time.sleep(1.2)

    # ---------------- 安装包清理（保留下载 开关）----------------
    def _clean_pkg(self, idx, pkg):
        """未勾选「保留下载的安装包」时：装成功后删掉对应的下载文件。

        只动 cache 目录里的东西（下载的包 / ZIP 里解出来的安装程序），
        绝不碰程序本身。
        """
        if self.keep_download:
            return
        removed = []
        cache = os.path.abspath(updater.CACHE_DIR)
        for key in ("path", "nested_from"):
            p = pkg.get(key)
            if not p:
                continue
            ap = os.path.abspath(p)
            if not ap.lower().startswith(cache.lower() + os.sep):
                continue
            if os.path.isfile(ap):
                updater._safe_remove(ap)
                if not os.path.exists(ap):
                    removed.append(os.path.basename(ap))
        self._prune_empty_dirs(os.path.join(cache, "_extract"))
        self.q.put(("drop_download", idx))
        if removed:
            self.log("  按设置删除安装包：%s" % "、".join(removed))

    @staticmethod
    def _prune_empty_dirs(root):
        if not os.path.isdir(root):
            return
        for cur, _dirs, _files in os.walk(root, topdown=False):
            if os.path.normcase(cur) == os.path.normcase(root):
                continue
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
            except OSError:
                pass

    # ---------------- 其它 ----------------
    def open_config(self):
        try:
            os.startfile(CONFIG)  # noqa: S606
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def restart_admin(self):
        if self.busy:
            return
        try:
            import ctypes
            bat = os.path.join(BASE, "一键更新.bat")
            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "cmd.exe", '/c "%s"' % bat, None, 1)
            if ret > 32:
                self.log("\n已请求以管理员身份重启，请在新窗口继续操作。")
                self.root.after(900, self.root.destroy)
            else:
                self.log("\n管理员重启已取消。")
        except Exception as e:
            messagebox.showerror("重启失败", str(e))


def main():
    if sys.platform != "win32":
        print("第一版仅支持 Windows。")
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
