import subprocess
import sys
import time
import re
import threading
import webbrowser
import os
from pyngrok import ngrok, conf

def run_uvicorn():
    # Uvicornサーバーを起動（コンソールなしでエラー落ちしないようログ出力に変更し、ファイルは開いたままにする）
    out_file = open("server_log.txt", "w", encoding="utf-8")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        stdout=out_file,
        stderr=subprocess.STDOUT
    )

def start_tunnel():
    time.sleep(2) # サーバー立ち上がりを待つ
    
    # 既存のngrokプロセスをPython側からも確実に強制キルする
    ngrok.kill()
    time.sleep(1)
    
    # ご提供いただいたNgrokの認証トークンを設定
    ngrok.set_auth_token("3ANagfDhHgbCzvzFs7Pbqaqt9It_2ynNWSXSb35GYjbjnCCLQ")
    
    # ご提供いただいた一生使える完全固定ドメインを指定
    domain = "overquietly-noninterchangeable-faustina.ngrok-free.dev"
    
    # max_retries
    for attempt in range(15):
        try:
            # ngrokでトンネルを開く
            http_tunnel = ngrok.connect(8000, domain=domain)
            url = http_tunnel.public_url
            
            # ファイルにURLを保存（ログ用など）
            with open("latest_url.txt", "w", encoding="utf-8") as f:
                f.write(url)
            
            # ローカルサーバーが完全に立ち上がるのを待つ
            time.sleep(3)
            webbrowser.open(url)
            
            return http_tunnel
        except Exception as e:
            ngrok.kill()
            with open("server_log.txt", "a", encoding="utf-8") as f:
                f.write(f"\n[Attempt {attempt+1}/15] Ngrok server-side domain lock detected. Waiting for cloud release... Error: {e}\n")
            # クラウドサーバーが過去のトンネル破棄を認識するまで待つ（1回5秒 × 15回 = 最大75秒）
            time.sleep(5)
            
    return None

def main():
    # サーバープロセスの起動
    uvicorn_thread = threading.Thread(target=run_uvicorn, daemon=True)
    uvicorn_thread.start()

    # トンネルプロセスの起動とURL取得・ブラウザオープン
    tunnel = start_tunnel()

    try:
        # メインスレッドを生かしておく
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        ngrok.kill()
        pass

if __name__ == "__main__":
    main()
