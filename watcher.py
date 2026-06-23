import os
import time
import shutil
import uuid
import json
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import PIL.Image
from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    print("Warning: pillow_heif is not installed. HEIC support is disabled.", flush=True)
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

# List of models for fallback pattern (最強モデル順)
MODELS_TO_TRY = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash"
]

if API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    genai.configure(api_key=API_KEY)

# Configure directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_IMPORT_DIR = os.path.join(BASE_DIR, "auto_import")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

os.makedirs(AUTO_IMPORT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

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

    generation_config = {"temperature": 0.1, "response_mime_type": "application/json"}
    
    prompt = """
    あなたは世界最高峰の名刺解析AIです。
    画像がスマホのカメラで撮影されたもので、多少の歪み、影、反射、ピンぼけがあっても、文字として認識できるものはすべて執念深く読み取ってください。
    
    画像の中に複数の名刺が並んでいる場合は、それぞれを個別のデータとして抽出してください。
    
    出力は必ず以下のJSON配列（リスト）形式のみとしてください。
    [
      {
        "name": "氏名（最優先で読み取ること）",
        "company_name": "会社名/法人名（ロゴや文字から特定）",
        "department": "所属部署",
        "title": "役職",
        "phone_number": "電話番号",
        "email": "メールアドレス",
        "address": "住所",
        "memo": "その他、URLやSNSアカウントなどがあればメモに記載"
      }
    ]
    
    ※もし文字が全く読み取れない場合でも、空のオブジェクトを返さず、可能な限りの断片を拾ってください。
    """

    try:
        with open(image_path, "rb") as f:
            file_data = f.read()
        if image_path.lower().endswith(".pdf"):
            pdf_document = fitz.open(stream=file_data, filetype="pdf")
            if len(pdf_document) == 0: return None
            page = pdf_document[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            pil_image = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            pdf_document.close()
        else:
            pil_image = PIL.Image.open(io.BytesIO(file_data))
            if pil_image.mode in ("RGBA", "P", "CMYK"):
                pil_image = pil_image.convert("RGB")
    except Exception as e:
        print(f"Watcher: Invalid image file {image_path}: {e}")
        return None

    for model_name in MODELS_TO_TRY:
        try:
            print(f"--- [watcher.py] Trying OCR model: {model_name} ---", flush=True)
            model = genai.GenerativeModel(model_name=model_name, generation_config=generation_config)
            response = model.generate_content([prompt, pil_image])
            
            cleaned_text = response.text.strip()
            if cleaned_text.startswith("```json"): cleaned_text = cleaned_text[7:]
            if cleaned_text.startswith("```"): cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"): cleaned_text = cleaned_text[:-3]
            parsed_data = json.loads(cleaned_text.strip())
            
            if isinstance(parsed_data, dict): parsed_data = [parsed_data]
            return parsed_data
        except Exception as e:
            print(f"Watcher: Fallback warning: Model {model_name} failed. Error: {e}")
            continue
    return None

def process_file(src_path: str):
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.pdf', '.heic', '.heif']:
        return

    print(f"Watcher: Processing -> {src_path}")
    ocr_results = perform_ocr(src_path)
    if not ocr_results:
        print(f"Watcher: OCR completely failed for {src_path}. Registering as blank card to allow manual entry.")
        ocr_results = [{}]

    new_filename = f"{uuid.uuid4()}{ext}"
    final_image_path = None

    if supabase_client:
        try:
            with open(src_path, "rb") as f:
                supabase_client.storage.from_("cards").upload(new_filename, f.read())
            final_image_path = supabase_client.storage.from_("cards").get_public_url(new_filename)
        except Exception as e:
            print(f"Watcher: Supabase upload failed: {e}")

    if not final_image_path:
        dest_path = os.path.join(UPLOAD_DIR, new_filename)
        shutil.move(src_path, dest_path)
        final_image_path = f"/uploads/{new_filename}"

    db = database.SessionLocal()
    try:
        registered_count = 0
        for idx, ocr_result in enumerate(ocr_results):
            name = ocr_result.get("name")
            company = ocr_result.get("company_name")
            
            # 名前も会社名もない場合
            if not name and not company:
                if idx == 0:
                    # 1件目（画像本体）の場合は、写真を残すために「OCR失敗」として登録する
                    name = "OCR解析失敗 (要手動入力)"
                    company = ""
                    ocr_result["memo"] = "文字が読み取れませんでした。画像を確認して手動で入力してください。"
                else:
                    # 2件目以降（PDFの余分な白紙ページなど）は無視する（ゴミデータ防止）
                    print(f"Watcher: Skipping empty extra entry in {src_path}")
                    continue

            db_card = models.DBBusinessCard(
                name=name,
                company_name=company,
                department=ocr_result.get("department"),
                title=ocr_result.get("title"),
                phone_number=ocr_result.get("phone_number"),
                email=ocr_result.get("email"),
                address=ocr_result.get("address"),
                memo=f"[自動登録] {ocr_result.get('memo', '')}",
                image_path=final_image_path
            )
            db.add(db_card)
            registered_count += 1
            
        if registered_count > 0:
            db.commit()
            print(f"Watcher: Successfully registered {registered_count} card(s).")
        else:
            print(f"Watcher: No valid card data found in {src_path}, skipping commit.")
    except Exception as e:
        db.rollback()
        print(f"Watcher: DB Error: {e}")
    finally:
        db.close()

class BusinessCardHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.src_path,)).start()

def start_watching():
    event_handler = BusinessCardHandler()
    observer = Observer()
    observer.schedule(event_handler, AUTO_IMPORT_DIR, recursive=False)
    observer.start()
    print(f"Watcher: Started monitoring directory '{AUTO_IMPORT_DIR}'")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    start_watching()
