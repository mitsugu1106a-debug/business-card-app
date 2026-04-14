import os, google.generativeai as genai, dotenv
dotenv.load_dotenv()
key = os.getenv('GEMINI_API_KEY')
print(f'Key starts with: {key[:5]}...' if key else 'No key')
try:
    genai.configure(api_key=key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    print(model.generate_content('hi').text)
except Exception as e:
    print(f'ERROR: {e}')
