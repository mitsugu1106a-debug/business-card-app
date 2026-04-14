import sqlite3
import os
import fitz
import PIL.Image
import io

db_path = "business_cards.db"
uploads_dir = "uploads"

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("SELECT id, image_path FROM business_cards WHERE image_path LIKE '%.pdf'")
rows = cursor.fetchall()

fixed_count = 0
for row in rows:
    card_id = row[0]
    pdf_path = row[1]
    
    # pdf_path is like /uploads/xxxx.pdf
    filename = os.path.basename(pdf_path)
    full_pdf_path = os.path.join(uploads_dir, filename)
    
    if os.path.exists(full_pdf_path):
        try:
            doc = fitz.open(full_pdf_path)
            if len(doc) > 0:
                page = doc[0]
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                
                new_filename = filename.replace('.pdf', '.png')
                new_full_path = os.path.join(uploads_dir, new_filename)
                
                with open(new_full_path, "wb") as f:
                    f.write(pix.tobytes("png"))
                    
                doc.close()
                
                new_image_path = f"/uploads/{new_filename}"
                cursor.execute("UPDATE business_cards SET image_path = ? WHERE id = ?", (new_image_path, card_id))
                print(f"Fixed {card_id}: {filename} -> {new_filename}")
                fixed_count += 1
        except Exception as e:
            print(f"Failed to convert {filename}: {e}")

conn.commit()
conn.close()

print(f"Fixed {fixed_count} records.")
