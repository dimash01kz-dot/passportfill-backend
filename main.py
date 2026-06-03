from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import base64
import os
import json
from typing import Optional

app = FastAPI(title="PassportFill API")

# Allow requests from Chrome extension and website
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

PASSPORT_PROMPT = """Ты эксперт по чтению паспортов. Извлеки все данные из этого паспорта и верни ТОЛЬКО JSON без лишнего текста.

Верни JSON в таком формате:
{
  "last_name": "ФАМИЛИЯ латиницей",
  "first_name": "ИМЯ латиницей",
  "middle_name": "ОТЧЕСТВО если есть",
  "birth_date": "ДД.ММ.ГГГГ",
  "passport_series": "серия (обычно одна буква N для казахских)",
  "passport_number": "номер паспорта (только цифры)",
  "expire_date": "ДД.ММ.ГГГГ дата окончания",
  "iin": "ИИН 12 цифр если есть",
  "gender": "M или F",
  "citizenship": "KAZ или RUS и т.д.",
  "tourist_type": "MR для мужчины взрослого, MRS для женщины взрослой, CHD для ребёнка до 12 лет",
  "nationality": "национальность"
}

Если какого-то поля нет — поставь null. Верни ТОЛЬКО JSON, без объяснений."""


@app.get("/")
def root():
    return {"status": "PassportFill API работает", "version": "1.0"}


@app.post("/extract")
async def extract_passport(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    # Validate API key from header
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API ключ не передан")

    api_key = authorization.replace("Bearer ", "")

    # TODO: проверить api_key в базе данных и списать паспорт
    # Пока принимаем любой непустой ключ
    if not api_key:
        raise HTTPException(status_code=401, detail="Неверный API ключ")

    # Read file
    file_bytes = await file.read()

    # Check size (max 10MB)
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (макс 10МБ)")

    # Determine media type
    content_type = file.content_type or "image/jpeg"
    if content_type not in ["image/jpeg", "image/png", "image/webp", "application/pdf"]:
        raise HTTPException(status_code=400, detail="Поддерживаются JPG, PNG, PDF")

    # Convert to base64
    file_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        if content_type == "application/pdf":
            # PDF document
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": file_b64
                            }
                        },
                        {"type": "text", "text": PASSPORT_PROMPT}
                    ]
                }]
            )
        else:
            # Image
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": content_type,
                                "data": file_b64
                            }
                        },
                        {"type": "text", "text": PASSPORT_PROMPT}
                    ]
                }]
            )

        # Parse response
        response_text = message.content[0].text.strip()

        # Clean up JSON if needed
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        passport_data = json.loads(response_text)

        # File is never saved — zero data retention
        del file_bytes
        del file_b64

        return {"success": True, "data": passport_data}

    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Не удалось прочитать паспорт. Попробуйте более чёткое фото.")
    except anthropic.APIError as e:
        raise HTTPException(status_code=500, detail=f"Ошибка AI: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")


@app.get("/health")
def health():
    return {"ok": True}
