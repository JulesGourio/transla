"""docx_images: what is sent to the vision endpoint."""

import asyncio
import base64
import io
from unittest.mock import patch

from PIL import Image

from server.services.translation import docx_images as D
from tests.docx_factory import build_docx, para


def _image_bytes(fmt):
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(buf, format=fmt)
    return buf.getvalue()


def _media_docx(name, data):
    return build_docx(para('x'), {f'word/media/{name}': data})


def _ocr(name, data):
    sent = []

    async def fake(host, token, endpoint, messages, max_tokens=0):
        sent.append(messages[0]['content'][1]['image_url']['url'])
        return {'text': 'Texte lu'}, {'input_tokens': 1, 'output_tokens': 1}

    with patch.object(D, 'call_llm_json', fake):
        results, _ = asyncio.run(D.ocr_docx_images(_media_docx(name, data), [name], 'h', 't', 'ep'))
    return results, sent


def test_a_gif_is_converted_to_png_before_it_is_sent():
    results, sent = _ocr('image1.gif', _image_bytes('GIF'))
    assert [r['text'] for r in results] == ['Texte lu']
    header, b64 = sent[0].split(',', 1)
    assert header == 'data:image/png;base64'
    assert Image.open(io.BytesIO(base64.b64decode(b64))).format == 'PNG'


def test_png_and_jpeg_are_sent_unchanged_with_their_own_type():
    png = _image_bytes('PNG')
    _, sent = _ocr('image1.png', png)
    assert sent[0] == 'data:image/png;base64,' + base64.b64encode(png).decode()
    jpg = _image_bytes('JPEG')
    _, sent = _ocr('image2.jpeg', jpg)
    assert sent[0].startswith('data:image/jpeg;base64,')
