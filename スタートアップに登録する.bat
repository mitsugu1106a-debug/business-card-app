@echo off
chcp 65001 >nul
echo ---------------------------------------------------
echo 名刺管理アプリを「スタートアップ」に登録します。
echo ---------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "TARGET_BAT=%SCRIPT_DIR%名刺管理アプリ起動.bat"
set "SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\名刺管理アプリ起動.lnk"

:: PowerShellを使って安全にショートカットを作成
powershell -Command "$wshell = New-Object -ComObject WScript.Shell; $shortcut = $wshell.CreateShortcut('%SHORTCUT_PATH%'); $shortcut.TargetPath = '%TARGET_BAT%'; $shortcut.WorkingDirectory = '%SCRIPT_DIR%'; $shortcut.Description = 'Business Card App'; $shortcut.Save()"

echo.
echo スタートアップへの登録が完了しました！
echo 次回パソコン起動時から、自動的に裏側で立ち上がります。
echo ---------------------------------------------------
pause
