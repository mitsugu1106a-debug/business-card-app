@echo off
chcp 65001 >nul
echo ---------------------------------------------------
echo モニター（画面）は消えるが、PC自体はスリープしないように
echo Windowsの電源設定を変更します。
echo ---------------------------------------------------

:: 【設定1】画面が消えるまでの時間（AC電源接続時＝15分に設定）
powercfg /change monitor-timeout-ac 15

:: 【設定2】PCがスリープするまでの時間（AC電源接続時＝0＝スリープしない）
powercfg /change standby-timeout-ac 0

:: （※もしノートPCなどでバッテリー駆動時の設定も変える場合は以下を使用）
powercfg /change monitor-timeout-dc 15
powercfg /change standby-timeout-dc 0

echo.
echo 設定が完了しました！
echo （15分間操作がないと画面は真っ暗になりますが、アプリは裏で動き続けます）
echo ---------------------------------------------------
pause
