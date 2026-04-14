import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("GEMINI_API_KEY")
print(f"Key loaded: {'YES' if key else 'NO'}")
if key:
    print(f"Starts with: {key[:4]}")
    print(f"Length: {len(key)}")
    if key == "YOUR_API_KEY_HERE":
        print("WARNING: Key is still the placeholder value.")
        exit(1)

genai.configure(api_key=key)
try:
    model = genai.GenerativeModel("gemini-1.5-flash")
    resp = model.generate_content("hello")
    print("SUCCESS: API call worked.")
except Exception as e:
    print(f"FAILED WITH ERROR: {e}")
