@echo off
chcp 65001 >nul
setlocal
set "BASH=C:\Program Files\Git\bin\bash.exe"
if not exist "%BASH%" (
  echo [FATAL] 找不到 Git Bash：%BASH%
  pause & exit /b 1
)
echo ============================================
echo  创建五个 Bot（工作助理 + 四专家）
echo  前置：DSH 必须已经完全退出
echo ============================================
"%BASH%" -lc "cd '/d/work/产品类/个人产品/bot-2/JuJing/bot-team' && bash tools/dsh_create_bots.sh"
echo.
echo ==== 完成，窗口保留 60 秒供查看 ====
timeout /t 60 >nul
