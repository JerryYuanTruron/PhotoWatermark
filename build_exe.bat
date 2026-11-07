@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 PhotoWatermark.exe

echo 使用 Python 3.12 + PyInstaller 打包单文件免安装程序，并写入 BUILD_INFO.txt ...
py -3 build.py

if %errorlevel%==0 (
    echo.
    echo 打包完成： dist\PhotoWatermark.exe
    echo 建议继续运行： py -3 tests\validate_frozen.py
) else (
    echo.
    echo 打包失败，请检查上方错误信息。
)
pause
