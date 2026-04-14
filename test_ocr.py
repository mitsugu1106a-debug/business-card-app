import PIL.Image
import watcher

def test():
    print("Creating test image...", flush=True)
    img = PIL.Image.new('RGB', (100, 100), color='red')
    img.save('test_img.jpg')
    print("Running OCR...", flush=True)
    res = watcher.perform_ocr('test_img.jpg')
    print("==== FINAL RESULT ====", flush=True)
    print(res, flush=True)

if __name__ == "__main__":
    test()
