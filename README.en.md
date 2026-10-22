# One-Click Updater · 一键更新

> Check, download and install updates for **every** app on your Windows machine — in one window.
> 一个把电脑上所有软件一次性检查、下载、装完的 Windows 图形化更新器。

[English](README.en.md) · [中文](README.md)

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D4?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

![Screenshot](docs/screenshot.png)

---

## Why

`winget upgrade --all` works, until it doesn't:

1. **You can't see what's happening** — a wall of scrolling text, and when something fails you get an HRESULT with no explanation.
2. **Portable apps are invisible** — the extract-and-run tools (ripgrep, tiny utilities) never show up in winget.
3. **Installers are a zoo** — MSIX throws `0x80073D02`, a ZIP gets executed as an EXE and throws `WinError 193`, vendor-custom bootstrappers silently ignore `/S` and pop a GUI you then wait on forever.

This tool turns the whole pipeline into three visible steps — **scan → download → install** — with progress, logs, and plain-language failure reasons.

## Features

| | |
|---|---|
| **Full inventory** | Registry uninstall keys + `winget list` + portable directories, merged and de-duplicated |
| **Only what needs updating** | Up-to-date apps are hidden; a row disappears the moment it's installed |
| **Three-way version check** | winget manifest / GitHub Release / executable `ProductVersion` — marked "unknown" instead of guessed |
| **Multi-connection download** | Parallel HTTP Range + live speed/progress + cancel at any time |
| **Integrity verification** | Length and SHA-256 checked against the manifest; truncated responses are detected and the partial file removed |
| **Network aware** | Proxy and direct connections raced; automatic mirror fallback for GitHub |
| **Two install modes** | *Sequential* (open installers one by one) or *silent* (framework-appropriate switches) |
| **MSIX handling** | Precisely closes the old package's processes to avoid `0x80073D02` |
| **ZIP handling** | Portable → extract & overwrite (with backup); nested installer → extract and run the inner EXE |
| **Trustworthy results** | Re-reads the registry version after install instead of trusting the exit code |
| **Ignore list** | Hide apps you never want to update, restore them anytime |

## Requirements

- Windows 10 1809+ / Windows 11
- Python 3.9+ with `tcl/tk` (tkinter)
- **No third-party packages** — standard library only

```bat
git clone https://github.com/<your-name>/oneclick-updater.git
cd oneclick-updater
一键更新.bat
```

## Usage

1. Launch — it scans automatically and lists every app that has an update.
2. Uncheck what you don't want, or hit **不更新 (Ignore)** to hide it permanently.
3. **下载更新 (Download)** → wait for the progress bar.
4. **安装更新 (Install) ▾**
   - *Sequential* — opens one installer at a time, waits for you to finish, then opens the next. Success is verified by version, not by exit code.
   - *Silent* — no UI for Inno / NSIS / MSI / MSIX, which have documented switches.
5. If elevation is needed, it offers to restart as administrator and run the batch silently.

> UI tips: drag the grey bar above the list to change row height; hover the column separator in the header (cursor turns ↔) and drag to resize columns; double-click a separator to auto-fit.

## Installer matrix

| Type | Detection | Switch / action |
|---|---|---|
| Inno Setup | binary contains `Inno Setup Setup Data` | `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` |
| NSIS | `NullsoftInst` / `Nullsoft.NSIS` | `/S` |
| InstallShield | `InstallShield` | `/s /v/qn` |
| WiX Burn | `WixBundle` / `BurnStub` | `/quiet /norestart` |
| MSI | `.msi` | `msiexec /i /qn /norestart` |
| MSIX / APPX | magic bytes + `AppxManifest.xml` | close old processes → `Add-AppxPackage -ForceApplicationShutdown` |
| ZIP (portable) | `PK\x03\x04`, no `AppxManifest.xml` | extract & overwrite, per-file backup |
| ZIP (nested installer) | contains `*installer*.exe` / `setup*.exe` | extract inner installer, then handle as above |
| Vendor custom | none of the above (QQ's Hummer, Epic Online Services) | don't guess — open the GUI and verify by version |

Switches are not guessed: the binary is scanned for framework signatures, and when nothing matches the tool says so instead of throwing `/S` at it and waiting for a timeout.

## Configuration

`config.json` (generated on first run, git-ignored):

| Key | Meaning |
|---|---|
| `portable_scan_dirs` | Directories scanned for portable apps |
| `portable_apps` | Manually registered portable apps (can use GitHub `owner/repo` + `asset` glob) |
| `row_height` / `col_widths` / `col_manual` | UI row height and column widths (saved when you drag) |
| `only_updatable` | Show only apps that need updating |
| `keep_download` | Keep downloaded installers (off = delete after a successful install) |
| `ignored` | Ignore list, matched case-insensitively by id / exe / name |
| `winget_timeout` | Timeout per winget call (seconds) |

## Limitations

- Windows only (`ctypes`, `winreg`, MSIX APIs).
- Vendor-custom installers genuinely have no silent switch — clicking through is unavoidable.
- MSIX apps held open by another session are skipped rather than killed blindly.
- No drivers, no Windows components, no Windows Update.

## License

[MIT](LICENSE)
