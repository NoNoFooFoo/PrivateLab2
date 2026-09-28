import io
import os
from pathlib import Path
import re
from typing import Union

_OCR_ENGINE = None
_ENGINE_TYPE = None  # 'ddddocr' oppure 'paddle'


def get_ocr_engine():
  """Inizializza ed esegue il caching dell'engine OCR in memoria.

  Priorità a ddddocr (ultra-veloce, 15MB) con fallback protetto su PaddleOCR.
  """
  global _OCR_ENGINE, _ENGINE_TYPE
  if _OCR_ENGINE is not None:
    return _OCR_ENGINE if _OCR_ENGINE is not False else None

  # 1. Tentativo prioritario con ddddocr (specializzato in Captcha)
  try:
    import ddddocr

    _OCR_ENGINE = ddddocr.DdddOcr(show_ad=False)
    _ENGINE_TYPE = "ddddocr"
    return _OCR_ENGINE
  except ImportError:
    pass
  except Exception as e:
    print(f"[OCR INIT] Avviso inizializzazione ddddocr: {e}")

  # 2. Fallback su PaddleOCR (versione stabile PP-OCRv4)
  try:
    from paddleocr import PaddleOCR

    _OCR_ENGINE = PaddleOCR(
        lang="en",
        ocr_version="PP-OCRv4",
        device="cpu",
        show_log=False,
    )
    _ENGINE_TYPE = "paddle"
    return _OCR_ENGINE
  except ImportError:
    pass
  except Exception as e:
    print(f"[OCR INIT] Avviso inizializzazione PaddleOCR: {e}")

  # Se nessun motore è installato, disattiva silenziosamente senza crashare
  _OCR_ENGINE = False
  return None


def preprocess_captcha_image(img):
  """Binarizzazione e contrasto per massimizzare la lettura sui captcha camerali."""
  try:
    from PIL import ImageEnhance

    # 1. Converti in scala di grigi
    gray = img.convert("L")
    # 2. Aumenta il contrasto per staccare i caratteri dallo sfondo
    enhancer = ImageEnhance.Contrast(gray)
    enhanced = enhancer.enhance(2.0)
    # 3. Binarizzazione con soglia a 140
    threshold = 140
    binarized = enhanced.point(lambda p: 255 if p > threshold else 0)
    return binarized
  except Exception:
    return img


def recognize_text_from_bytes(image_input: Union[bytes, str, Path]) -> str:
  """Accetta bytes immagine, percorso file o Path e restituisce il testo alfanumerico pulito."""
  if not image_input:
    return ""

  try:
    from PIL import Image
  except ImportError:
    print(
        "[OCR ERROR] Libreria Pillow non trovata. Esegui: pip install Pillow"
    )
    return ""

  try:
    # 1. Caricamento e normalizzazione dell'immagine
    if isinstance(image_input, (str, Path)) and Path(image_input).exists():
      img = Image.open(str(image_input))
    elif isinstance(image_input, (bytes, bytearray)):
      img = Image.open(io.BytesIO(image_input))
    else:
      return ""

    engine = get_ocr_engine()
    if engine is None:
      return ""

    # 2. Pre-elaborazione antirumore
    clean_img = preprocess_captcha_image(img)

    # 3. Esecuzione con ddddocr
    if _ENGINE_TYPE == "ddddocr":
      buffer = io.BytesIO()
      clean_img.save(buffer, format="PNG")
      raw_text = engine.classification(buffer.getvalue())
      return re.sub(r"[^A-Za-z0-9]", "", raw_text).strip()

    # 4. Esecuzione con PaddleOCR
    elif _ENGINE_TYPE == "paddle":
      import numpy as np

      np_img = np.array(clean_img.convert("RGB"))
      results = engine.ocr(np_img, cls=False)
      recognized_texts = []
      if results and isinstance(results, list):
        for block in results:
          if not block:
            continue
          for line in block:
            if isinstance(line, (list, tuple)) and len(line) >= 2:
              text_info = line[1]
              if isinstance(text_info, (list, tuple)):
                recognized_texts.append(str(text_info[0]))
              elif isinstance(text_info, str):
                recognized_texts.append(text_info)

      full_text = "".join(recognized_texts)
      return re.sub(r"[^A-Za-z0-9]", "", full_text).strip()

  except Exception as e:
    print(f"[OCR SERVICE ERROR]: {e}")
    return ""

  return ""
