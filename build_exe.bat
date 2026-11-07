@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 PhotoWatermark.exe

echo 使用 Python 3.12 + PyInstaller 打包单文件免安装程序...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name PhotoWatermark ^
    --distpath dist --workpath build --specpath . ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageDraw ^
    --hidden-import PIL.ImageFont ^
    --hidden-import PIL.ImageFilter ^
    --hidden-import PIL.ImageOps ^
    --hidden-import PIL.ImageChops ^
    --collect-submodules src ^
    main.py

if %errorlevel%==0 (
    echo.
    echo 打包完成： dist\PhotoWatermark.exe
) else (
    echo.
    echo 打包失败，请检查上方错误信息。
)
pause
