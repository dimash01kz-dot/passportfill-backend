# PassportFill Backend

API сервер для чтения паспортов через Claude AI.

## Файлы

- `main.py` — основной сервер
- `requirements.txt` — зависимости Python
- `Procfile` — инструкция для Railway

## Деплой на Railway

1. Зайдите на railway.app
2. Нажмите "New Project" → "Deploy from GitHub"
3. Или используйте "Deploy from local" и загрузите эту папку
4. В настройках добавьте переменную окружения:
   - ANTHROPIC_API_KEY = ваш_ключ

## Эндпоинты

POST /extract — принимает фото паспорта, возвращает данные
GET /health — проверка работоспособности
