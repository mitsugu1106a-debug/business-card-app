import os
import time
import shutil
import uuid
import json
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import PIL.Image
import fitz # PyMuPDF
import io
import traceback

import google.generativeai as genai
from dotenv import load_dotenv

import models
import database
from sqlalchemy.orm import Session

# Load env limits
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    genai.configure(api_key=API_KEY)

# Configure directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_IMPORT_DIR = os.path.join(BASE_DIR, "auto_import")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(AUTO_IMPORT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# List of models for fallback pattern
MODELS_TO_TRY = [
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash"
]

# Supabase Initialization (for cloud storage)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Watcher: Failed to initialize Supabase: {e}")

def perform_ocr(image_path: str):
    if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
        print("Watcher: OCR skipped due to missing API KEY.")
        return None

    genai.configure(api_key=API_KEY)
    generation_config = {"temperature": 0.1, "response_mime_type": "application/json"}
    
    prompt = """
    あなたは高精度な名刺読み取りAIです。入力された画像に【複数の名刺（例: 8枚など）】が写っている場合、それぞれの名刺ごとに完全に独立したデータとして抽出してください。
    【重要・厳守】:
    - ある名刺の「氏名」と、別の名刺の「住所」や「会社名」を絶対に混同したり、使い回したりしてはいけません。
    - 抽出する「address」は、必ずその「name」が記載されているのと同じ1枚の名刺枠内に書かれている住所のみを記載してください。
    - 指定したJSONの【配列（リスト）形式】でのみ出力してください。
    - 読み取れない項目や存在しない項目は null または空文字にしてください。
    [
      {
        "name": "氏名",
        "company_name": "会社名/法人名",
        "department": "所属部署",
        "title": "役職",
        "phone_number": "電話番号 (固定電話と携帯電話の両方がある場合は「固定: 03-... / 携帯: 090-...」のように記載)",
        "email": "メールアドレス",
        "address": "住所（都道府県、市区町村、番地、建物名など）",
        "memo": "その他、WebサイトのURL、事業内容などを自由にまとめたテキスト"
      }
    ]
    """

    try:
        # Load image/pdf into memory and close file handle immediately
        with open(image_path, "rb") as f:
            file_data = f.read()
            
        if image_path.lower().endswith(".pdf"):
            pdf_document = fitz.open(stream=file_data, filetype="pdf")
            if len(pdf_document) == 0:
                print(f"Watcher: Empty PDF {image_path}")
                return None
            page = pdf_document[0]
            zoom_matrix = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=zoom_matrix)
            pil_image = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            pdf_document.close()
        else:
            pil_image = PIL.Image.open(io.BytesIO(file_data))
    except Exception as e:
        print(f"Watcher: Invalid image file {image_path}: {e}")
        return None

    response = None
    for model_name in MODELS_TO_TRY:
        try:
            print(f"--- [watcher.py] Trying OCR model: {model_name} ---", flush=True)
            model = genai.GenerativeModel(model_name=model_name, generation_config=generation_config)
            response = model.generate_content([prompt, pil_image])
            print(f"[watcher.py] OCR Success using model: {model_name}", flush=True)
            print(f"[watcher.py] Raw Response:\n{response.text}\n", flush=True)
            break
        except Exception as e:
            print(f"[watcher.py] Fallback warning: Model {model_name} failed. Error: {type(e).__name__} - {e}", flush=True)
            pass # Fallback to next

    if response:
        try:
            cleaned_text = response.text.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.startswith("```"):
                cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            parsed_data = json.loads(cleaned_text.strip())
            
            if isinstance(parsed_data, dict):
                parsed_data = [parsed_data]
            elif not isinstance(parsed_data, list):
                parsed_data = []
                
            return parsed_data
        except Exception as e:
            print(f"========== [watcher.py] JSON Parsing Error ==========", flush=True)
            print(f"Problematic Text: {response.text}", flush=True)
            traceback.print_exc()
            print("=====================================================", flush=True)
            return None
    else:
        print("[watcher.py] FATAL: All configured models failed.", flush=True)
    return None

def process_file(src_path: str):
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.pdf']:
        print(f"Watcher: Skipping non-image file {src_path}")
        return

    print(f"Watcher: Processing new file -> {src_path}")
    
    # Wait for file to be completely written (basic polling lock-check)
    file_stable = False
    for _ in range(10):
        try:
            with open(src_path, 'ab'):
                file_stable = True
                break
        except IOError:
            time.sleep(1)
            
    if not file_stable:
        print(f"Watcher: File {src_path} is locked by another process.")
        return

    # OCR process
    ocr_results = perform_ocr(src_path)
    if not ocr_results:
        ocr_results = [{}]

    # Move/Upload image
    new_filename = f"{uuid.uuid4()}{ext}"
    final_image_path = None

    # クラウドストレージ(Supabase)へのアップロード試行
    if supabase_client:
        try:
            # PDFの場合は1ページ目をサムネイル化してアップロード
            if ext == '.pdf':
                try:
                    doc = fitz.open(src_path)
                    if len(doc) > 0:
                        page = doc[0]
                        pix = page.get_pixmap(matrix=fitz.Matrix(2,2))
                        png_data = pix.tobytes("png")
                        new_filename = new_filename.replace('.pdf', '.png')
                        supabase_client.storage.from_("cards").upload(new_filename, png_data)
                        final_image_path = supabase_client.storage.from_("cards").get_public_url(new_filename)
                    doc.close()
                except Exception as e:
                    print(f"Watcher: PDF thumbnail generation failed: {e}")
            else:
                # 通常画像
                with open(src_path, "rb") as f:
                    supabase_client.storage.from_("cards").upload(new_filename, f.read())
                final_image_path = supabase_client.storage.from_("cards").get_public_url(new_filename)
            
            # 処理済みローカルファイルを削除
            if os.path.exists(src_path):
                os.remove(src_path)
            print(f"Watcher: Successfully uploaded {new_filename} to Supabase.")
        except Exception as e:
            print(f"Watcher: Supabase upload failed: {e}. Falling back to local storage.")

    # クラウドアップロードに失敗した場合や設定がない場合、ローカルに保持（エフェメラル）
    if not final_image_path:
        dest_path = os.path.join(UPLOAD_DIR, new_filename)
        try:
            shutil.move(src_path, dest_path)
            final_image_path = f"/uploads/{new_filename}"
        except Exception as e:
            print(f"Watcher: Failed to hold file locally: {e}")
            return

    # DB Registration
    db = database.SessionLocal()
    try:
        for idx, ocr_result in enumerate(ocr_results):
            # 複数抽出された場合、画像は最初の一件のみ、それ以外は電子名刺
            db_card = models.DBBusinessCard(
                name=ocr_result.get("name"),
                company_name=ocr_result.get("company_name"),
                department=ocr_result.get("department"),
                title=ocr_result.get("title"),
                phone_number=ocr_result.get("phone_number"),
                email=ocr_result.get("email"),
                address=ocr_result.get("address"),
                memo=ocr_result.get("memo") and f"[一括登録]\n{ocr_result.get('memo', '')}" or "[一括登録]",
                image_path=final_image_path if idx == 0 else None
            )
            db.add(db_card)
        db.commit()
        print(f"Watcher: Successfully registered {len(ocr_results)} card(s) in DB.")
    except Exception as e:
        db.rollback()
        print(f"Watcher: DB Error: {e}")
    finally:
        db.close()


class BusinessCardHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            # Run in a separate thread to avoid blocking the observer
            threading.Thread(target=process_file, args=(event.src_path,)).start()
            
    def on_moved(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.dest_path,)).start()

def start_watching():
    event_handler = BusinessCardHandler()
    observer = Observer()
    observer.schedule(event_handler, AUTO_IMPORT_DIR, recursive=False)
    observer.start()
    print(f"Watcher: Started monitoring directory '{AUTO_IMPORT_DIR}'")
    
    # Keep the thread running
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

def process_all_pending():
    """auto_importフォルダ内の全画像ファイルを手動で一括処理する"""
    results = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }
    
    if not os.path.exists(AUTO_IMPORT_DIR):
        print(f"Watcher: Directory {AUTO_IMPORT_DIR} does not exist.")
        return results
        
    for filename in os.listdir(AUTO_IMPORT_DIR):
        src_path = os.path.join(AUTO_IMPORT_DIR, filename)
        if not os.path.isfile(src_path):
            continue
            
        ext = os.path.splitext(src_path)[1].lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.pdf']:
            print(f"Watcher: Skipping non-image/pdf file {src_path}")
            results["skipped"] += 1
            results["details"].append({"file": filename, "status": "skipped", "reason": "Not an image or pdf"})
            continue

        print(f"Watcher: Manually processing file -> {src_path}")
        try:
            # check file lock
            file_stable = False
            for _ in range(5):
                try:
                    with open(src_path, 'ab'):
                        file_stable = True
                        break
                except IOError:
                    time.sleep(0.5)
                    
            if not file_stable:
                print(f"Watcher: File {src_path} is locked by another process.")
                results["failed"] += 1
                results["details"].append({"file": filename, "status": "failed", "reason": "File locked"})
                continue
                
            # OCR process
            ocr_results = perform_ocr(src_path)
            if not ocr_results:
                ocr_results = [{}]

            # Move file to uploads
            new_filename = f"{uuid.uuid4()}{ext}"
            dest_path = os.path.join(UPLOAD_DIR, new_filename)
            shutil.move(src_path, dest_path)

            # DB Registration
            db = database.SessionLocal()
            try:
                for ocr_result in ocr_results:
                    db_card = models.DBBusinessCard(
                        name=ocr_result.get("name"),
                        company_name=ocr_result.get("company_name"),
                        department=ocr_result.get("department"),
                        title=ocr_result.get("title"),
                        phone_number=ocr_result.get("phone_number"),
                        email=ocr_result.get("email"),
                        address=ocr_result.get("address"),
                        memo=ocr_result.get("memo") and f"[手動インポート]\n{ocr_result.get('memo', '')}" or "[手動インポート]",
                        image_path=f"/uploads/{new_filename}"
                    )
                    db.add(db_card)
                db.commit()
                print(f"Watcher: Successfully registered {len(ocr_results)} card(s) from {new_filename}")
                results["processed"] += len(ocr_results)
                results["details"].append({"file": filename, "status": "success", "count": len(ocr_results)})
            except Exception as e:
                db.rollback()
                print(f"Watcher: DB Error: {e}")
                results["failed"] += 1
                results["details"].append({"file": filename, "status": "failed", "reason": f"DB Error: {e}"})
                
                # Undo move on failure? For now, leave it in uploads for safety (already moved).
            finally:
                db.close()
                
        except Exception as e:
            print(f"Watcher: Critical error processing file {src_path}: {e}")
            traceback.print_exc()
            results["failed"] += 1
            results["details"].append({"file": filename, "status": "failed", "reason": f"Process error: {e}"})

    return results
