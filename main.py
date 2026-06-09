from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
import anthropic
import base64
import os
import json
import traceback
import httpx
from typing import Optional

app = FastAPI(title="PassportFill API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
print(f"Supabase configured: URL={bool(SUPABASE_URL)}, KEY={bool(SUPABASE_SERVICE_KEY)}")
print(f"All env vars: {list(os.environ.keys())}")

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
  "issue_date": "ДД.ММ.ГГГГ дата выдачи паспорта",
  "issued_by": "кем выдан паспорт латинскими буквами (например MVDKAZ или MIA RK)",
  "iin": "ИИН 12 цифр если есть",
  "gender": "M или F",
  "citizenship": "KAZ или RUS и т.д.",
  "tourist_type": "MR для мужчины взрослого, MRS для женщины взрослой, CHD для ребёнка до 12 лет",
  "nationality": "национальность"
}

Если какого-то поля нет — поставь null. Верни ТОЛЬКО JSON, без объяснений."""


async def check_and_use_credit(api_key: str) -> dict:
    """Check API key in Supabase and deduct 1 credit"""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        # If Supabase not configured, allow all requests (dev mode)
        print("WARNING: Supabase not configured, skipping credit check")
        return {"success": True, "credits_left": 999}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SUPABASE_URL}/rest/v1/rpc/use_credit",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json"
            },
            json={"p_api_key": api_key},
            timeout=10.0
        )

        if response.status_code != 200:
            print(f"Supabase error: {response.status_code} {response.text}")
            return {"success": False, "error": "Ошибка проверки ключа"}

        return response.json()


@app.get("/")
def root():
    return {"status": "PassportFill API работает", "version": "1.0"}


@app.post("/extract")
async def extract_passport(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="API ключ не передан")

    api_key = authorization.replace("Bearer ", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Неверный API ключ")

    # Check credits in Supabase
    credit_result = await check_and_use_credit(api_key)
    if not credit_result.get("success"):
        error = credit_result.get("error", "Ошибка")
        if "кредит" in error.lower() or "недостаточно" in error.lower():
            raise HTTPException(status_code=402, detail="Недостаточно кредитов. Пополните баланс на passportfill.kz")
        raise HTTPException(status_code=401, detail=f"Неверный API ключ: {error}")

    print(f"Processing file: {file.filename}, type: {file.content_type}, credits_left: {credit_result.get('credits_left')}")

    file_bytes = await file.read()

    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Файл слишком большой (макс 10МБ)")

    content_type = file.content_type or "image/jpeg"
    if content_type not in ["image/jpeg", "image/png", "image/webp", "application/pdf"]:
        print(f"Unsupported type: {content_type}, forcing image/jpeg")
        content_type = "image/jpeg"

    file_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print("Calling Claude API...")

        if content_type == "application/pdf":
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}},
                    {"type": "text", "text": PASSPORT_PROMPT}
                ]}]
            )
        else:
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=1024,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": file_b64}},
                    {"type": "text", "text": PASSPORT_PROMPT}
                ]}]
            )

        response_text = message.content[0].text.strip()
        print(f"Claude response: {response_text[:200]}")

        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        passport_data = json.loads(response_text.strip())
        del file_bytes
        del file_b64

        return {
            "success": True,
            "data": passport_data,
            "credits_left": credit_result.get("credits_left")
        }

    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        raise HTTPException(status_code=422, detail="Не удалось прочитать паспорт. Попробуйте более чёткое фото.")
    except anthropic.APIError as e:
        print(f"Anthropic API error: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка AI: {str(e)}")
    except Exception as e:
        print(f"Unexpected error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")


@app.get("/health")
def health():
    return {"ok": True}
