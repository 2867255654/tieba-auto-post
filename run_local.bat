@echo off
REM ============================================================
REM 贴吧游戏资讯每日摘抄 - 本地运行脚本
REM 用法：
REM   1. 双击本文件 = 立即运行一次（采集 + 摘抄 + [发帖]）
REM   2. 配合「Windows 任务计划程序」可实现每天自动跑
REM 配置全在同目录的 .env 里（含 BDUSS / FORUM_NAMES / POST_MODE 等）
REM ============================================================
setlocal

REM 切换到本文件所在目录（项目根），保证相对路径正确
cd /d "%~dp0"

REM 托管 Python 解释器（WorkBuddy 默认 venv）
set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

REM —— 可选：本地采集海外源（PCGamer/GameSpot 等）若慢或空，可走代理 ——
REM 取消下面两行注释，并改成你的代理端口（Clash Verge 通常 7897 / V2RayN 10809）
REM set "HTTP_PROXY=http://127.0.0.1:7897"
REM set "HTTPS_PROXY=http://127.0.0.1:7897"

if not exist "%PY%" (
    echo [错误] 找不到 Python 解释器：
    echo   %PY%
    echo 请确认 WorkBuddy 托管 Python 已安装（或改用你自己的 python 路径）。
    pause
    exit /b 1
)

echo ================================================
echo   贴吧游戏资讯摘抄 - 开始运行
echo   时间：%date% %time%
echo ================================================
"%PY%" main.py
set "RC=%errorlevel%"

echo ================================================
if %RC% equ 0 (
    echo   运行结束：成功（退出码 0）
) else (
    echo   运行结束：异常（退出码 %RC%）
)
echo ================================================
if %RC% neq 0 pause
endlocal
