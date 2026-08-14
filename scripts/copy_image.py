import shutil, os
src = r'C:\Users\Administrator\AppData\Roaming\bee_ai_test\chat-images\img_v3_0214b_8ee5be62-3749-4049-8066-cfed959506ag.jpg'
dst = r'C:\Users\Administrator\.bee\plugins\.my-plugin\skills\kol-opinion-analyzer\data\images\wu2198_20260807_dow.jpg'
os.makedirs(os.path.dirname(dst), exist_ok=True)
shutil.copy2(src, dst)
print(f'[OK] {os.path.getsize(dst)} bytes → {dst}')
