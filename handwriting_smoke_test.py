
from pathlib import Path
from PIL import Image, ImageDraw
import tempfile
import main

with tempfile.TemporaryDirectory() as td:
    p1 = Path(td)/'a.jpg'
    p2 = Path(td)/'b.jpg'
    im = Image.new('RGB',(300,160),'white')
    d = ImageDraw.Draw(im)
    d.line((30,80,260,80), fill='black', width=4)
    d.text((40,55),'test sign', fill='black')
    im.save(p1)
    im.save(p2)
    f1 = main.handwriting_feature_vector(p1)
    f2 = main.handwriting_feature_vector(p2)
    assert f1 and f2
    assert main.handwriting_similarity(f1,f2) > 0.9
print('handwriting smoke tests passed')
