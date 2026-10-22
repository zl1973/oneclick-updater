@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 找一个「带 tkinter 的」Python：优先 Windows 官方 py 启动器，其次 PATH 里的 python
set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import tkinter" >nul 2>&1
    if not errorlevel 1 set "PY=%%~C"
  )
)
if not defined PY (
  echo.
  echo [错误] 没找到可用的 Python（需要 3.9+ 且包含 tkinter）
  echo.
  echo 安装方法（任选其一）：
  echo   1^) 微软商店 / 官网安装 Python 3.12，安装时勾选 "tcl/tk and IDLE"
  echo   2^) 命令行执行：winget install Python.Python.3.12
  echo.
  pause
  exit /b 1
)

%PY% "%~dp0main.py" %*
if errorlevel 1 pause
