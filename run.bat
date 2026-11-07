@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 批量照片时间水印处理程序 v1.0.0

echo 正在检查运行环境（tkinter + Pillow）...

py -3 -c "import tkinter, PIL" >nul 2>&1
if %errorlevel%==0 (
    py -3 main.py
    goto :end
)

python -c "import tkinter, PIL" >nul 2>&1
if %errorlevel%==0 (
    python main.py
    goto :end
)

echo.
echo [错误] 未找到同时具备 tkinter 与 Pillow 的 Python 3.10+ 环境。
echo 请任选一种方式：
echo   1) 直接使用免安装版 dist\PhotoWatermark.exe
echo   2) 安装 Python 3.10+ 后执行： pip install -r requirements.txt
echo.
pause

:end
