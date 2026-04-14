@echo off
chcp 65001 >nul
title 名刺管理アプリ 起動プログラム
cd /d "%~dp0"

echo ===================================================
echo   名刺管理アプリを起動しています...
echo ===================================================
echo.

:: 過去のバックグラウンドプロセスが残っていたら強制終了してファイルを解放する
echo [クリーンアップ] 過去の起動プロセスをリセットしています...
taskkill /F /IM python.exe /T >nul 2>&1
taskkill /F /IM pythonw.exe /T >nul 2>&1
taskkill /F /IM wscript.exe /T >nul 2>&1
taskkill /F /IM ssh.exe /T >nul 2>&1
taskkill /F /IM ngrok.exe /T >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000') do (
    taskkill /F /PID %%a >nul 2>&1
)

:: Python実行環境（venv）の検証と作成
:: (USBなどで別PCに移動した場合、既存のvenvはパスが合わず壊れるため、エラーが出たら作り直す仕組み)
python -c "import sys; sys.exit(0)" >nul 2>&1
if not exist "venv\Scripts\activate.bat" (
    echo [初回セットアップ] Python仮想環境を作成しています... (数分かかる場合があります)
    if exist "venv" rmdir /s /q "venv"
    python -m venv venv
) else (
    :: activate.batはあるが、他のPCから持ってきてパスが壊れている場合の対策
    venv\Scripts\python.exe -c "import sys; sys.exit(0)" >nul 2>&1
    if errorlevel 1 (
        echo [環境修復] PCが変わったため仮想環境を再構築しています... (数分かかります)
        rmdir /s /q "venv"
        python -m venv venv
    )
)

:: 仮想環境の有効化
call venv\Scripts\activate.bat

:: requirements.txt が存在するか確認し、なければ作成・インストール
if exist "requirements.txt" (
    echo [開始準備] 必要なパッケージを確認しています...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo エラー：パッケージのインストールに失敗しました。インターネット接続などを確認してください。
        pause
        exit /b
    )
)

echo.
echo [起動準備完了] サーバープログラムを開始します。
echo ※この黒い画面は開いたままにしておいてください。閉じるとアプリが終了します。
echo.

:: 自分自身のIPアドレスを表示してアクセスURLを案内
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /C:"IPv4 Address" /C:"IPv4 アドレス"') do set IP=%%i
if not "%IP%"=="" set IP=%IP: =%

echo ===================================================
echo ✓ アプリが起動しました！
echo   以下のURLをブラウザで開いてご利用ください：
echo   http://localhost:8000/
echo ===================================================
echo.



:: 外部公開機能を含んだサーバーを完全にバックグラウンドで起動するためのスクリプト作成
echo Set WshShell = CreateObject("WScript.Shell") > run_hidden.vbs
echo WshShell.Run "cmd /c .\venv\Scripts\python.exe start_with_share.py", 0, False >> run_hidden.vbs

:: 作成したスクリプトを実行して裏側でサーバーを起動
wscript run_hidden.vbs

echo.
echo ===================================================
echo ✓ アプリが裏側で起動しました！
echo   この黒い画面は自動で閉じます。
echo   【重要】数秒後に自動でブラウザが開き、専用のURLが表示されます。
echo   そのURLをスマホ等にLINEやメールで送れば、どこからでもアクセス可能です！
echo ===================================================

:: サーバーが立ち上がり、URLが取得されてブラウザが開くのを待つため少し待機
timeout /t 5 >nul

exit
