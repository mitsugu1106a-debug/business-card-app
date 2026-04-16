# VERSION: 2026-04-14-REPAIR-FINAL
import base64
import secrets
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Body, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
import models, database
from pydantic import BaseModel, ConfigDict
from datetime import datetime
import os
import uuid
import shutil
import json
import google.generativeai as genai
from dotenv import load_dotenv
import PIL.Image
import fitz  # PyMuPDF
import io
import traceback
import threading
from contextlib import asynccontextmanager
# watcher はローカル専用（クラウドでは不要）
try:
    import watcher
    HAS_WATCHER = True
except ImportError:
    HAS_WATCHER = False
import re
import csv
import zipfile
from fastapi.responses import StreamingResponse
from supabase import create_client, Client

# Supabase Initialization
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Failed to intialize Supabase Client: {e}")


# Load environment variables (.env)
load_dotenv()

# Configure Gemini API (初期設定はLifespanと各リクエストに委ねるため簡易化)
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    genai.configure(api_key=API_KEY)

# Using flash/pro for fast multimodal processing
generation_config = {"temperature": 0.1, "response_mime_type": "application/json"}

# Create tables
models.Base.metadata.create_all(bind=database.engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # アプリ起動時にフォルダ監視を別スレッドで開始（ローカルのみ）
    if HAS_WATCHER:
        watch_thread = threading.Thread(target=watcher.start_watching, daemon=True)
        watch_thread.start()
    yield
    # シャットダウン時の処理はここに追加

app = FastAPI(title="Business Card API", lifespan=lifespan)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Basic Auth Configuration for internal usage
VALID_USERS = {
    "admin": "admin123",
    "member": "member123",
    "test": "test123"
}

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
        
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return Response(
            content="Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Business Card App"'}
        )
    
    try:
        decoded = base64.b64decode(auth_header.split(" ")[1]).decode("utf-8")
        username, _, password = decoded.partition(":")
        
        if username in VALID_USERS and secrets.compare_digest(password, VALID_USERS[username]):
            return await call_next(request)
    except Exception:
        pass

    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Business Card App"'}
    )

# Ensure uploads directory exists and mount it
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

class TagOut(BaseModel):
    id: str
    name: str
    model_config = ConfigDict(from_attributes=True)

class AttachmentOut(BaseModel):
    id: str
    file_name: str
    file_path: str
    uploaded_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Pydantic output schema
class BusinessCardOut(BaseModel):
    id: str
    name: Optional[str] = None
    company_name: Optional[str] = None
    department: Optional[str] = None
    title: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    exchange_date: Optional[str] = None
    memo: Optional[str] = None
    image_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    tags: List[TagOut] = []
    attachments: List[AttachmentOut] = []

    model_config = ConfigDict(from_attributes=True)

class BulkDeleteRequest(BaseModel):
    card_ids: List[str]

class BulkExportRequest(BaseModel):
    card_ids: List[str]
    charset: Optional[str] = "utf-8-sig"

class MergeRequest(BaseModel):
    primary_card_id: str
    secondary_card_id: str

class ChangeHistoryOut(BaseModel):
    id: str
    card_id: str
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    change_type: str
    changed_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Helper for saving uploaded file
def save_upload_file(upload_file: UploadFile) -> str:
    # 拡張子を取得
    ext = os.path.splitext(upload_file.filename)[1].lower()
    # 一意なファイル名を作成
    filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)
        
    final_upload_path = file_path
    final_filename = filename

    # PDFの場合はブラウザの<img>タグで表示できないため、1ページ目を画像化(PNG)して保存する
    if ext == '.pdf':
        try:
            doc = fitz.open(file_path)
            if len(doc) > 0:
                page = doc[0]
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                png_filename = filename.replace('.pdf', '.png')
                png_file_path = os.path.join(UPLOAD_DIR, png_filename)
                with open(png_file_path, "wb") as f:
                    f.write(pix.tobytes("png"))
                doc.close()
                final_upload_path = png_file_path
                final_filename = png_filename
        except Exception as e:
            print(f"Failed to generate PNG from PDF: {e}")
            
    # Supabaseが設定されていればクラウドの bucket 'cards' に保存
    if supabase_client:
        try:
            with open(final_upload_path, "rb") as f:
                res = supabase_client.storage.from_("cards").upload(final_filename, f.read())
            public_url = supabase_client.storage.from_("cards").get_public_url(final_filename)
            # ローカルファイルを削除
            if os.path.exists(file_path): os.remove(file_path)
            if final_upload_path != file_path and os.path.exists(final_upload_path): os.remove(final_upload_path)
            return public_url
        except Exception as e:
            print(f"Supabase upload failed: {e}. Falling back to local.")
            
    return f"/uploads/{final_filename}"

def delete_image_file(image_path: str):
    if not image_path:
        return
    if image_path.startswith("http") and supabase_client:
        filename = image_path.split("/")[-1].split("?")[0]
        try:
            supabase_client.storage.from_("cards").remove([filename])
        except Exception as e:
            print(f"Failed to delete {filename} from Supabase: {e}")
    else:
        filename = os.path.basename(image_path)
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

def sync_tags(db: Session, db_card: models.DBBusinessCard, tags_str: str):
    if tags_str is None:
        return
    
    # 既存のタグの紐付けを解除（空文字の場合＝全タグ削除にも対応）
    db_card.tags.clear()
    
    # タグ名をパースして紐付け
    tag_names = [t.strip() for t in tags_str.split(',') if t.strip()]
    for tn in set(tag_names):
        tag = db.query(models.Tag).filter(models.Tag.name == tn).first()
        if not tag:
            tag = models.Tag(name=tn)
            db.add(tag)
        db_card.tags.append(tag)

@app.post("/upload-async/")
async def upload_async(images: List[UploadFile] = File(...)):
    """
    スマホ等から画像を「投げ込む」非同期エンドポイント。
    画像を受け取り、auto_importフォルダに保存するだけで即座にHTTP 200を返す。
    OCR解析はバックグラウンドのwatcherプロセスへ委譲する（待たせない）。
    """
    import_dir = os.path.join(BASE_DIR, "auto_import")
    os.makedirs(import_dir, exist_ok=True)
    
    saved_files = []
    
    for upload_file in images:
        if not upload_file.filename:
            continue
            
        ext = os.path.splitext(upload_file.filename)[1]
        
        # スマホからのアップロード時に推測しやすいよう、タイムスタンプ形式にする
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:6]
        filename = f"mobile_upload_{timestamp_str}_{unique_id}{ext}"
        file_path = os.path.join(import_dir, filename)
        
        try:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(upload_file.file, buffer)
            saved_files.append(filename)
            print(f"[{datetime.now()}] [Async Upload] Image dropped to auto_import: {filename}", flush=True)
        except Exception as e:
            print(f"Error saving uploaded file {filename}: {e}", flush=True)
            # 全体としては止めずに次へ
    
    if not saved_files:
         raise HTTPException(status_code=400, detail="No valid images were uploaded.")
         
    return {"message": f"Successfully received {len(saved_files)} images for background processing."}

# OCR Endpoint
@app.post("/ocr/")
async def analyze_business_card(image: UploadFile = File(...)):
    if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
        raise HTTPException(status_code=500, detail="Gemini API Key is not configured in .env file.")
    
    try:
        # 画像・PDFメモリ読み込み
        file_data = await image.read()
        filename = image.filename.lower() if image.filename else ""
        
        if filename.endswith(".pdf"):
            # PDFの場合、1ページ目を画像（PNG）に変換する
            pdf_document = fitz.open(stream=file_data, filetype="pdf")
            if len(pdf_document) == 0:
                raise Exception("The uploaded PDF is empty.")
            page = pdf_document[0] # 1ページ目のみ処理
            # 2倍の解像度でレンダリング（OCR精度向上のため）
            zoom_matrix = fitz.Matrix(2, 2)
            pix = page.get_pixmap(matrix=zoom_matrix)
            pil_image = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            pdf_document.close()
        else:
            # 通常の画像
            pil_image = PIL.Image.open(io.BytesIO(file_data))
        
        # プロンプト設定: JSONスキーマに沿った配列（リスト）形式での回答を強制
        prompt = """
        あなたは高精度な名刺読み取りAIです。入力された名刺画像から、写っている全ての名刺の情報を抽出し、必ず指定したJSONの【配列（リスト）形式】でのみ出力してください。
        読み取れない項目や存在しない項目は null または空文字にしてください。

        【重要な追加指示】
        - 名刺の表面にペンや鉛筆で手書きされた日付（例: 2025.3.15、R7.3.15、2025/03/15 など）がある場合、それは名刺交換日です。exchange_date に YYYY-MM-DD 形式で記載してください。
        - 手書きで追記された電話番号や携帯番号も、印刷された番号と同様に読み取って phone_number に含めてください。
        - 手書き文字と印刷文字の両方を正確に読み取ってください。

        [
          {
            "name": "氏名",
            "company_name": "会社名/法人名",
            "department": "所属部署",
            "title": "役職",
            "phone_number": "電話番号 (固定電話と携帯電話の両方がある場合は「固定: 03-... / 携帯: 090-...」のように記載。手書きの番号も含む)",
            "email": "メールアドレス",
            "address": "住所（都道府県、市区町村、番地、建物名など）",
            "exchange_date": "名刺交換日 (手書きの日付がある場合はYYYY-MM-DD形式で記載。なければ空文字)",
            "memo": "その他、WebサイトのURL、事業内容、手書きのメモ書きなどを自由にまとめたテキスト"
          }
        ]
        """
        
        # 2026年現在の最強モデルから順に試行するフォールバックリスト
        models_to_try = [
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-3.1-flash-lite-preview",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash"
        ]
        response = None
        last_error = None
        selected_model_name = None
        
        for model_name in models_to_try:
            try:
                print(f"--- [main.py] Trying OCR model: {model_name} ---", flush=True)
                model = genai.GenerativeModel(model_name=model_name, generation_config=generation_config)
                response = model.generate_content([prompt, pil_image])
                selected_model_name = model_name
                print(f"[main.py] OCR Success using model: {model_name}", flush=True)
                break # 成功した場合はループを抜ける
            except Exception as model_err:
                print(f"[main.py] Fallback warning: Model {model_name} failed. Error: {type(model_err).__name__} - {model_err}", flush=True)
                last_error = model_err
                
        if not response:
            error_msg = f"All configured models failed. Last error: {last_error}"
            print(f"[main.py] FATAL: {error_msg}", flush=True)
            traceback.print_exc()
            raise Exception(error_msg)
            
        print(f"[main.py] Raw Response Text:\n{response.text}\n", flush=True)
        
        # 結果をパース
        try:
            cleaned_text = response.text.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.startswith("```"):
                cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            parsed_data = json.loads(cleaned_text.strip())
            
            # AIが単体のDictを返してきた場合のフェールセーフ
            if isinstance(parsed_data, dict):
                parsed_data = [parsed_data]
            elif not isinstance(parsed_data, list):
                parsed_data = []
                
            return {"cards": parsed_data}
        except Exception as json_err:
            print(f"========== [main.py] JSON Parsing Error ==========", flush=True)
            print(f"Problematic Text: {response.text}", flush=True)
            traceback.print_exc()
            print("==================================================", flush=True)
            raise Exception(f"JSON Parse Error: {json_err}")
            
    except Exception as e:
        print(f"========== [main.py] OCR Critical Error ==========", flush=True)
        print(f"Exception Type: {type(e).__name__}", flush=True)
        print(f"Message: {e}", flush=True)
        traceback.print_exc()
        print("==================================================", flush=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cards/", response_model=BusinessCardOut)
def create_card(
    name: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    department: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    phone_number: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    exchange_date: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(database.get_db)
):
    image_path = None
    if image and image.filename:
        image_path = save_upload_file(image)

    db_card = models.DBBusinessCard(
        name=name,
        company_name=company_name,
        department=department,
        title=title,
        phone_number=phone_number,
        email=email,
        address=address,
        exchange_date=exchange_date,
        memo=memo,
    )
    db.add(db_card)
    sync_tags(db, db_card, tags)
    db.commit()
    db.refresh(db_card)
    return db_card

@app.get("/cards/")
def read_cards(page: int = 1, per_page: int = 500, search: str = "", db: Session = Depends(database.get_db)):
    query = db.query(models.DBBusinessCard)

    if search:
        sf = f"%{search}%"
        query = query.filter(
            (models.DBBusinessCard.name.ilike(sf)) |
            (models.DBBusinessCard.company_name.ilike(sf)) |
            (models.DBBusinessCard.memo.ilike(sf)) |
            (models.DBBusinessCard.department.ilike(sf)) |
            (models.DBBusinessCard.email.ilike(sf)) |
            (models.DBBusinessCard.phone_number.ilike(sf)) |
            (models.DBBusinessCard.address.ilike(sf))
        )

    total = query.count()

    if per_page == 0:
        cards = query.order_by(models.DBBusinessCard.created_at.desc()).all()
        total_pages = 1
        page = 1
    else:
        total_pages = max(1, (total + per_page - 1) // per_page)
        cards = query.order_by(models.DBBusinessCard.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    return {
        "items": [BusinessCardOut.model_validate(c) for c in cards],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages
    }

@app.get("/cards/{card_id}", response_model=BusinessCardOut)
def read_card(card_id: str, db: Session = Depends(database.get_db)):
    card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card

@app.put("/cards/{card_id}", response_model=BusinessCardOut)
def update_card(
    card_id: str,
    name: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    department: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    phone_number: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    address: Optional[str] = Form(None),
    exchange_date: Optional[str] = Form(None),
    memo: Optional[str] = Form(None),
    tags: Optional[str] = Form(""),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(database.get_db)
):
    db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if db_card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    # 変更履歴を記録
    field_map = {
        'name': name, 'company_name': company_name, 'department': department,
        'title': title, 'phone_number': phone_number, 'email': email,
        'address': address, 'exchange_date': exchange_date, 'memo': memo
    }
    for fn, nv in field_map.items():
        ov = getattr(db_card, fn) or ""
        nv_str = nv or ""
        if ov != nv_str:
            db.add(models.ChangeHistory(card_id=card_id, field_name=fn, old_value=ov, new_value=nv_str, change_type="update"))

    image_path = db_card.image_path
    if image and image.filename:
        image_path = save_upload_file(image)

    db_card.name = name
    db_card.company_name = company_name
    db_card.department = department
    db_card.title = title
    db_card.phone_number = phone_number
    db_card.email = email
    db_card.address = address
    db_card.exchange_date = exchange_date
    db_card.memo = memo
    db_card.image_path = image_path

    sync_tags(db, db_card, tags)

    db.commit()
    db.refresh(db_card)
    return db_card

@app.delete("/cards/{card_id}", response_model=dict)
def delete_card(card_id: str, db: Session = Depends(database.get_db)):
    db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if db_card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    
    # 画像ファイルがあれば削除
    if db_card.image_path:
        delete_image_file(db_card.image_path)
    
    db.delete(db_card)
    db.commit()
    return {"message": "Card successfully deleted"}

@app.post("/cards/bulk-delete", response_model=dict)
def bulk_delete_cards(request: BulkDeleteRequest, db: Session = Depends(database.get_db)):
    deleted_count = 0
    not_found_count = 0
    for card_id in request.card_ids:
        db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
        if db_card:
            # 画像ファイルがあれば削除
            if db_card.image_path:
                delete_image_file(db_card.image_path)
            db.delete(db_card)
            deleted_count += 1
        else:
            not_found_count += 1
            
    db.commit()
    return {"message": f"Successfully deleted {deleted_count} cards.", "not_found": not_found_count}

@app.get("/cards/duplicates/")
def find_duplicates(db: Session = Depends(database.get_db)):
    cards = db.query(models.DBBusinessCard).all()
    name_groups = {}
    email_groups = {}
    for card in cards:
        if card.name and card.name.strip():
            key = card.name.strip()
            name_groups.setdefault(key, []).append(card)
        if card.email and card.email.strip():
            key = card.email.strip().lower()
            email_groups.setdefault(key, []).append(card)

    duplicate_groups = []
    seen_card_sets = set()
    for name, group in name_groups.items():
        if len(group) > 1:
            ids = frozenset(c.id for c in group)
            if ids not in seen_card_sets:
                seen_card_sets.add(ids)
                duplicate_groups.append({"match_type": "name", "match_value": name, "cards": [BusinessCardOut.model_validate(c) for c in sorted(group, key=lambda x: x.created_at, reverse=True)]})
    for email, group in email_groups.items():
        if len(group) > 1:
            ids = frozenset(c.id for c in group)
            if ids not in seen_card_sets:
                seen_card_sets.add(ids)
                duplicate_groups.append({"match_type": "email", "match_value": email, "cards": [BusinessCardOut.model_validate(c) for c in sorted(group, key=lambda x: x.created_at, reverse=True)]})
    return {"duplicate_groups": duplicate_groups, "total_groups": len(duplicate_groups)}

@app.post("/cards/merge/")
def merge_cards(request: MergeRequest, db: Session = Depends(database.get_db)):
    primary = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == request.primary_card_id).first()
    secondary = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == request.secondary_card_id).first()
    if not primary or not secondary:
        raise HTTPException(status_code=404, detail="Card not found")
    fields = ['name', 'company_name', 'department', 'title', 'phone_number', 'email', 'address', 'exchange_date', 'memo']
    for field in fields:
        ov = getattr(primary, field) or ""
        nv = getattr(secondary, field) or ""
        if nv:
            db.add(models.ChangeHistory(card_id=primary.id, field_name=field, old_value=ov, new_value=f"[マージ元] {nv}", change_type="merge"))
        if not ov and nv:
            setattr(primary, field, nv)
    if secondary.image_path:
        delete_image_file(secondary.image_path)
    db.delete(secondary)
    db.commit()
    db.refresh(primary)
    return {"message": "統合が完了しました", "primary_card": BusinessCardOut.model_validate(primary)}

@app.get("/cards/{card_id}/history")
def get_card_history(card_id: str, db: Session = Depends(database.get_db)):
    history = db.query(models.ChangeHistory).filter(models.ChangeHistory.card_id == card_id).order_by(models.ChangeHistory.changed_at.desc()).all()
    return [ChangeHistoryOut.model_validate(h) for h in history]

@app.post("/cards/export-csv")
def export_csv_selected(request: BulkExportRequest, db: Session = Depends(database.get_db)):
    cards = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id.in_(request.card_ids)).order_by(models.DBBusinessCard.created_at.asc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "company_name", "department", "title", "phone_number", "email", "address", "exchange_date", "memo", "image_path"])
    for card in cards:
        writer.writerow([card.id, card.name or "", card.company_name or "", card.department or "", card.title or "", card.phone_number or "", card.email or "", card.address or "", card.exchange_date or "", card.memo or "", card.image_path or ""])
    output.seek(0)
    content_bytes = output.getvalue().encode('cp932', errors='replace')
    return StreamingResponse(iter([content_bytes]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=business_cards_selected.csv"})

@app.post("/export-vcard")
def export_vcard(request: BulkExportRequest, db: Session = Depends(database.get_db)):
    return generate_vcard_response(request.card_ids, request.charset, db)

@app.post("/export-vcard-form")
def export_vcard_form(card_ids: str = Form(...), charset: str = Form("utf-8-sig"), db: Session = Depends(database.get_db)):
    card_ids_list = [c.strip() for c in card_ids.split(",") if c.strip()]
    return generate_vcard_response(card_ids_list, charset, db)

def generate_vcard_response(card_ids: List[str], charset: Optional[str], db: Session):
    cards = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id.in_(card_ids)).all()
    
    # 登録日時が新しい順にソート（重複時に最新のものを優先するため）
    cards.sort(key=lambda x: x.created_at, reverse=True)
    
    unique_cards = []
    
    for card in cards:
        is_duplicate = False
        for current_card in unique_cards:
            # 名前が一致するかチェック
            if card.name and current_card.name and card.name.strip() == current_card.name.strip():
                # メアドか電話番号のどちらかが一致するかチェック
                phone_match = card.phone_number and current_card.phone_number and card.phone_number.strip() == current_card.phone_number.strip()
                email_match = card.email and current_card.email and card.email.strip() == current_card.email.strip()
                
                if phone_match or email_match:
                    is_duplicate = True
                    break
        
        if not is_duplicate:
            unique_cards.append(card)

    # リクエストの文字コードによってvCardのバージョンとタグを切り替える
    charset = charset if charset else "utf-8-sig"
    is_sjis = (charset == "shift_jis" or charset == "cp932")

    vcf_lines = []
    for card in unique_cards:
        vcf_lines.append("BEGIN:VCARD")
        
        # Shift-JISを強制する場合、一部のAndroid用（旧仕様）としてvCard 2.1形式にフォールバックさせ、
        # 各テキスト項目に明示的にCHARSET=SHIFT_JISタグを付与する。
        if is_sjis:
            vcf_lines.append("VERSION:2.1")
            c_tag = ";CHARSET=SHIFT_JIS"
        else:
            vcf_lines.append("VERSION:3.0")
            c_tag = ""
            
        # vCardでは「FN（表示名）」と「N（構造化された名前）」が絶対に必須項目
        name_val = card.name.strip() if card.name else ""
        company_val = card.company_name.strip() if card.company_name else ""
        
        # スマホのアドレス帳ですぐわかるよう「会社名 + 氏名」の形にする
        if company_val and name_val:
            display_name = f"{company_val} {name_val}"
        elif name_val:
            display_name = name_val
        elif company_val:
            display_name = company_val
        else:
            display_name = "名称未設定"
        
        vcf_lines.append(f"FN{c_tag}:{display_name}")
        vcf_lines.append(f"N{c_tag}:{display_name};;;;")
            
        if company_val:
            vcf_lines.append(f"ORG{c_tag}:{company_val}")
            
        if card.title:
            vcf_lines.append(f"TITLE{c_tag}:{card.title.strip()}")
            
        if card.department:
            vcf_lines.append(f"ROLE{c_tag}:{card.department.strip()}")
            
        if card.phone_number:
            # 電話番号の分離 (携帯:CELL と 固定:WORK を分ける)
            # OCRでくっついてしまった番号（例: 09000000000050212123）を文字数とプレフィックスで分割
            s = re.sub(r'[携帯固電話FfAaXxTteLlLl:：/／、,。.\\-−\s]+', ' ', card.phone_number)
            for p in s.split():
                clean_p = re.sub(r'[^\d]', '', p)
                while len(clean_p) > 0:
                    if clean_p.startswith('090') or clean_p.startswith('080') or clean_p.startswith('070'):
                        if len(clean_p) >= 11:
                            vcf_lines.append(f"TEL;TYPE=CELL,VOICE:{clean_p[:11]}")
                            clean_p = clean_p[11:]
                        else:
                            vcf_lines.append(f"TEL;TYPE=CELL,VOICE:{clean_p}")
                            break
                    elif clean_p.startswith('050'):
                        if len(clean_p) >= 11:
                            vcf_lines.append(f"TEL;TYPE=WORK,VOICE:{clean_p[:11]}")
                            clean_p = clean_p[11:]
                        else:
                            vcf_lines.append(f"TEL;TYPE=WORK,VOICE:{clean_p}")
                            break
                    elif clean_p.startswith('0'):
                        if len(clean_p) >= 10:
                            vcf_lines.append(f"TEL;TYPE=WORK,VOICE:{clean_p[:10]}")
                            clean_p = clean_p[10:]
                        else:
                            vcf_lines.append(f"TEL;TYPE=WORK,VOICE:{clean_p}")
                            break
                    else:
                        vcf_lines.append(f"TEL;TYPE=WORK,VOICE:{clean_p}")
                        break
            
        if card.email:
            email = card.email.replace('\n', ' ').replace('\r', '').strip()
            vcf_lines.append(f"EMAIL;TYPE=PREF,INTERNET:{email}")
            
        if card.memo:
            # vCardのメモ内の改行は \n でエスケープする
            memo = card.memo.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '').strip()
            vcf_lines.append(f"NOTE{c_tag}:{memo}")
            
        vcf_lines.append("END:VCARD")

    # RFC規格に準拠するため、改行コードは厳密なCRLFを使用する
    vcf_content = "\r\n".join(vcf_lines) + "\r\n"
    
    # リクエストから指定された文字コードでエンコード
    content_bytes = vcf_content.encode(charset, errors='replace')
    
    return StreamingResponse(
        iter([content_bytes]),
        media_type=f"text/vcard; charset={charset}",
        headers={
            "Content-Disposition": "attachment; filename=contacts.vcf"
        }
    )

@app.post("/export-thunderbird-csv")
def export_thunderbird_csv(request: BulkExportRequest, db: Session = Depends(database.get_db)):
    return generate_thunderbird_csv_response(request.card_ids, db)

@app.post("/export-thunderbird-csv-form")
def export_thunderbird_csv_form(card_ids: str = Form(...), db: Session = Depends(database.get_db)):
    card_ids_list = [c.strip() for c in card_ids.split(",") if c.strip()]
    return generate_thunderbird_csv_response(card_ids_list, db)

def generate_thunderbird_csv_response(card_ids: List[str], db: Session):
    if not card_ids:
        raise HTTPException(status_code=400, detail="No card IDs provided.")
        
    cards = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id.in_(card_ids)).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Thunderbird (Japanese) format headers
    headers = [
        "名", "姓", "表示名", "ニックネーム", "第1メールアドレス", "第2メールアドレス", 
        "スクリーンネーム", "勤務先電話番号", "自宅電話番号", "FAX番号", "ポケットベル", 
        "携帯電話番号", "自宅住所", "自宅住所2", "自宅市町村", "自宅都道府県", 
        "自宅郵便番号", "自宅国", "勤務先住所", "勤務先住所2", "勤務先市町村", 
        "勤務先都道府県", "勤務先郵便番号", "勤務先国", "勤務先名", "役職", "勤務先部署", 
        "Webページ1", "Webページ2", "誕生年", "誕生月", "誕生日", "カスタム1", 
        "カスタム2", "カスタム3", "カスタム4", "メモ"
    ]
    writer.writerow(headers)
    
    for card in cards:
        name_val = card.name.strip() if card.name else ""
        company_val = card.company_name.strip() if card.company_name else ""
        
        display_name = ""
        if company_val and name_val:
            display_name = f"{company_val} {name_val}"
        elif name_val:
            display_name = name_val
        elif company_val:
            display_name = company_val
            
        # Parse phone
        phone = card.phone_number or ""
        work_phone = ""
        cell_phone = ""
        if phone:
            s_nums = re.sub(r'[携帯固電話FfAaXxTteLlLl:：/／、,。.\\-−\s]+', ' ', phone)
            for p in s_nums.split():
                clean_p = re.sub(r'[^\d]', '', p)
                while len(clean_p) > 0:
                    if clean_p.startswith('090') or clean_p.startswith('080') or clean_p.startswith('070'):
                        cell_len = min(11, len(clean_p))
                        if not cell_phone:
                            cell_phone = clean_p[:cell_len]
                        clean_p = clean_p[cell_len:]
                    elif clean_p.startswith('050'):
                        work_len = min(11, len(clean_p))
                        if not work_phone:
                            work_phone = clean_p[:work_len]
                        clean_p = clean_p[work_len:]
                    elif clean_p.startswith('0'):
                        work_len = min(10, len(clean_p))
                        if not work_phone:
                            work_phone = clean_p[:work_len]
                        clean_p = clean_p[work_len:]
                    else:
                        if not work_phone:
                            work_phone = clean_p
                        break
        
        row = [
            name_val,        # 名
            "",              # 姓
            display_name,    # 表示名
            "",              # ニックネーム
            card.email or "",# 第1メールアドレス
            "",              # 第2メールアドレス
            "",              # スクリーンネーム
            work_phone,      # 勤務先電話番号
            "",              # 自宅電話番号
            "",              # FAX番号
            "",              # ポケットベル
            cell_phone,      # 携帯電話番号
            "",              # 自宅住所
            "",              # 自宅住所2
            "",              # 自宅市町村
            "",              # 自宅都道府県
            "",              # 自宅郵便番号
            "",              # 自宅国
            card.address or "", # 勤務先住所
            "",              # 勤務先住所2
            "",              # 勤務先市町村
            "",              # 勤務先都道府県
            "",              # 勤務先郵便番号
            "",              # 勤務先国
            company_val,     # 勤務先名
            card.title or "",# 役職
            card.department or "", # 勤務先部署
            "",              # Webページ1
            "",              # Webページ2
            "",              # 誕生年
            "",              # 誕生月
            "",              # 誕生日
            "",              # カスタム1
            "",              # カスタム2
            "",              # カスタム3
            "",              # カスタム4
            card.memo.replace('\\', '\\\\').replace('\n', ' ').strip() if card.memo else "" # メモ
        ]
        writer.writerow(row)
        
    output.seek(0)
    # Thunderbird accepts Shift-JIS natively in Japanese Windows
    content_bytes = output.getvalue().encode('cp932', errors='replace')
    
    return StreamingResponse(
        iter([content_bytes]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="thunderbird_contacts.csv"'
        }
    )

@app.post("/manual-import/", response_model=dict)
def manual_import(db: Session = Depends(database.get_db)):
    if not HAS_WATCHER:
        return {"message": "Manual import is not available on cloud.", "results": {}}
    results = watcher.process_all_pending()
    return {"message": f"Manual import completed.", "results": results}

@app.post("/export-csv")
def export_csv(request: BulkExportRequest, db: Session = Depends(database.get_db)):
    return generate_csv_response(request.card_ids, db)

@app.post("/export-csv-form")
def export_csv_form(card_ids: str = Form(...), db: Session = Depends(database.get_db)):
    card_ids_list = [c.strip() for c in card_ids.split(",") if c.strip()]
    return generate_csv_response(card_ids_list, db)

def generate_csv_response(card_ids: List[str], db: Session):
    if not card_ids:
        raise HTTPException(status_code=400, detail="No card IDs provided.")
        
    cards = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id.in_(card_ids)).order_by(models.DBBusinessCard.created_at.asc()).all()
    
    # メモリ上でCSVを作成
    output = io.StringIO()
    # CSV Data
    writer = csv.writer(output)
    
    # ヘッダー（英語カラム名だとあとでインポート時に扱いやすい）
    writer.writerow(["id", "name", "company_name", "department", "title", "phone_number", "email", "address", "exchange_date", "memo", "image_path", "tags", "attachments"])
    
    # データ行
    for card in cards:
        tags_str = ",".join([t.name for t in card.tags]) if card.tags else ""
        attachments_str = ",".join([a.file_name for a in card.attachments]) if card.attachments else ""
        
        writer.writerow([
            card.id,
            card.name or "",
            card.company_name or "",
            card.department or "",
            card.title or "",
            card.phone_number or "",
            card.email or "",
            card.address or "",
            card.exchange_date or "",
            card.memo or "",
            card.image_path or "",
            tags_str,
            attachments_str
        ])
    
    output.seek(0)
    
    # Outlook Classic等での文字化け（Mojibake）を防ぐため、強制的にShift-JIS (cp932) でエンコードする
    # 変換できない文字がある場合は 'replace' で '?' 等に置き込みエラーを防ぐ
    content_bytes = output.getvalue().encode('cp932', errors='replace')
    
    return StreamingResponse(
        iter([content_bytes]),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="business_cards_selected.csv"'
        }
    )


@app.post("/import-csv/")
async def import_csv(file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Invalid file format. Please upload a .csv file.")
    
    try:
        content = await file.read()
        
        text_content = ""
        try:
            # 1. まずUTF-8 (BOM付き含む)
            text_content = content.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                # 2. 失敗したら日本のWindowsで標準的なcp932 (Shift-JIS拡張)
                text_content = content.decode('cp932')
            except UnicodeDecodeError:
                # 3. それでもダメなら、不明な文字を?に置き換えて強引に読み込む（エラーで止めない）
                text_content = content.decode('cp932', errors='replace')
            
        csv_reader = csv.DictReader(io.StringIO(text_content))
        
        imported_count = 0
        updated_count = 0
        skipped_count = 0
        batch_count = 0
        BATCH_SIZE = 50
        
        for row in csv_reader:
            card_id = row.get("id")
            if not card_id:
                skipped_count += 1
                continue
                
            existing_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
            
            if existing_card:
                existing_card.name = row.get("name", existing_card.name)
                existing_card.company_name = row.get("company_name", existing_card.company_name)
                existing_card.department = row.get("department", existing_card.department)
                existing_card.title = row.get("title", existing_card.title)
                existing_card.phone_number = row.get("phone_number", existing_card.phone_number)
                existing_card.email = row.get("email", existing_card.email)
                existing_card.address = row.get("address", existing_card.address)
                existing_card.exchange_date = row.get("exchange_date", existing_card.exchange_date)
                existing_card.memo = row.get("memo", existing_card.memo)
                
                csv_image_path = row.get("image_path")
                if csv_image_path:
                    existing_card.image_path = csv_image_path.replace('\\', '/')
                    
                updated_count += 1
                target_card = existing_card
            else:
                new_card = models.DBBusinessCard(
                    id=card_id,
                    name=row.get("name", ""),
                    company_name=row.get("company_name", ""),
                    department=row.get("department", ""),
                    title=row.get("title", ""),
                    phone_number=row.get("phone_number", ""),
                    email=row.get("email", ""),
                    address=row.get("address", ""),
                    exchange_date=row.get("exchange_date", ""),
                    memo=row.get("memo", ""),
                    image_path=row.get("image_path", "").replace('\\', '/') if row.get("image_path") else ""
                )
                db.add(new_card)
                imported_count += 1
                target_card = new_card
            
            if "tags" in row:
                sync_tags(db, target_card, row.get("tags"))

            batch_count += 1
            if batch_count >= BATCH_SIZE:
                db.commit()
                batch_count = 0
                
        db.commit()
        return {"message": f"Import complete. Added: {imported_count}, Updated: {updated_count}, Skipped: {skipped_count}"}
        
    except Exception as e:
        print(f"Error during CSV import: {e}")
        traceback.print_exc()
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process CSV file: {str(e)}")

@app.get("/export-backup/")
def export_backup():
    """
    システムの完全バックアップ（データベース + 画像フォルダ）をZIPとしてダウンロードさせる。
    """
    memory_file = io.BytesIO()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. データベースファイルの追加
        db_path = os.path.join(BASE_DIR, "business_cards.db")
        if os.path.exists(db_path):
            zf.write(db_path, arcname="business_cards.db")
            
        # 2. アップロード画像群の追加 (Supabase優先)
        if supabase_client:
            try:
                bucket = supabase_client.storage.from_("cards")
                files = bucket.list()
                for file_info in files:
                    fn = file_info.get('name')
                    if fn and not fn.startswith('.'):
                        res = bucket.download(fn)
                        zf.writestr(f"uploads/{fn}", res)
            except Exception as e:
                print(f"Supabase backup fetch error: {e}")
                
        # 3. ローカルのアップロード画像群の追加
        if os.path.exists(UPLOAD_DIR):
            for root, dirs, files in os.walk(UPLOAD_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    # ZIP内のパス（フォルダ構造）を維持
                    arcname = os.path.relpath(file_path, BASE_DIR) 
                    zf.write(file_path, arcname=arcname)
                    
    memory_file.seek(0)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([memory_file.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=business_cards_full_backup_{timestamp}.zip"
        }
    )

from fastapi.responses import FileResponse

# Health check test endpoint (serve index.html)
@app.get("/")
def read_root():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"Hello": "World", "Status": "API is running but index.html not found"}

@app.get("/tags/", response_model=List[TagOut])
def get_tags(db: Session = Depends(database.get_db)):
    return db.query(models.Tag).order_by(models.Tag.name).all()

@app.post("/cards/{card_id}/attachments", response_model=AttachmentOut)
def upload_attachment(card_id: str, file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not db_card:
        raise HTTPException(status_code=404, detail="Card not found")
        
    file_path = save_upload_file(file)
    attachment = models.Attachment(
        card_id=card_id,
        file_name=file.filename,
        file_path=file_path
    )
    db.add(attachment)
    db.commit()
    db.refresh(attachment)
    return attachment

@app.delete("/attachments/{attachment_id}", response_model=dict)
def delete_attachment(attachment_id: str, db: Session = Depends(database.get_db)):
    attachment = db.query(models.Attachment).filter(models.Attachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
        
    delete_image_file(attachment.file_path)
        
    db.delete(attachment)
    db.commit()
    return {"message": "Attachment deleted"}
