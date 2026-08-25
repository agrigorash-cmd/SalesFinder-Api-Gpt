# SalesFinder → ChatGPT Connector (MVP)

Минимальный read-only коннектор для Custom GPT Actions.

## Что он делает

- хранит `SALESFINDER_EMAIL` и `SALESFINDER_PASSWORD` только на вашем сервере;
- автоматически логинится в SalesFinder и кеширует Bearer token;
- повторно авторизуется при 401/403;
- соблюдает интервал между запросами к SalesFinder;
- защищает сам коннектор отдельным `X-API-Key`;
- предоставляет GPT только аналитические read-only методы.

Доступные действия:

- `getProductInfo`
- `getProductOverview`
- `getProductDailyMetrics`
- `getProductKeywords`
- `searchCategories`
- `createCategoryReport`
- `getCategoryOverview`
- `getCategoryProducts`

## 1. Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:

```env
SALESFINDER_EMAIL=ваш_email
SALESFINDER_PASSWORD=ваш_пароль
CONNECTOR_API_KEY=случайный_длинный_ключ
```

Сгенерировать ключ:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Запуск:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
```

Проверка:

```bash
curl http://localhost:8000/health
```

Пример запроса:

```bash
curl -X POST http://localhost:8000/sf/product/overview \
  -H "X-API-Key: ВАШ_CONNECTOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "mp": "wb",
    "sku": [123456789],
    "date": "2026-08-01",
    "date2": "2026-08-25",
    "fbs": 0
  }'
```

## 2. Деплой

Самый простой вариант — любой сервис, умеющий запускать Docker-контейнер.

Нужно добавить секреты/Environment Variables:

- `SALESFINDER_EMAIL`
- `SALESFINDER_PASSWORD`
- `CONNECTOR_API_KEY`

Команда запуска уже прописана в `Dockerfile`.

Важно: для этого MVP держите **1 worker / 1 instance**, потому что ограничитель SalesFinder хранится в памяти процесса.

После деплоя получите адрес вроде:

```text
https://salesfinder-connector.example.com
```

Проверьте:

```text
https://salesfinder-connector.example.com/health
```

и OpenAPI:

```text
https://salesfinder-connector.example.com/openapi.json
```

## 3. Подключение к Custom GPT

В редакторе GPT:

1. Откройте **Actions → Create new action**.
2. В Schema импортируйте:
   `https://ВАШ-ДОМЕН/openapi.json`
3. Authentication:
   - тип: **API Key**
   - способ: **Custom header**
   - header: `X-API-Key`
   - value: значение `CONNECTOR_API_KEY`
4. Сохраните Action и протестируйте `getProductOverview`.

### Рекомендуемая инструкция для GPT

```text
Ты — аналитик маркетплейсов Wildberries и Ozon.
Для фактических данных о товарах и категориях всегда используй инструменты SalesFinder.

Правила:
- Не придумывай метрики, которых нет в ответах SalesFinder.
- Для анализа SKU обычно сначала вызывай getProductOverview.
- Если нужна динамика по дням, используй getProductDailyMetrics.
- Если нужны поисковые запросы и позиции, используй getProductKeywords.
- Для категории сначала searchCategories, затем createCategoryReport.
- После createCategoryReport используй reportId в getCategoryOverview или getCategoryProducts.
- Если отчет категории еще формируется, сообщи об этом и повтори получение отчета только после разумной паузы.
- При сравнении товаров явно указывай период и маркетплейс.
- Все финансовые выводы отделяй от фактов SalesFinder и помечай как расчет/интерпретацию.
```

## 4. Что можно спросить у GPT

```text
Проанализируй WB артикул 123456789 за последние 30 дней:
продажи, выручка, цена, остатки и динамика отзывов.
```

```text
Сравни WB артикулы 11111111, 22222222 и 33333333
за период 1–25 августа. Кто растет быстрее?
```

```text
Покажи поисковые запросы товара 123456789 и позиции по ним.
```

```text
Найди категорию "органайзеры для кухни" на WB,
создай отчет и покажи товары с выручкой от 1 000 000
и количеством отзывов до 300.
```

## Безопасность

- Не вставляйте SalesFinder email/password в GPT Instructions или Action schema.
- Не коммитьте `.env` в Git.
- Используйте длинный случайный `CONNECTOR_API_KEY`.
- Этот MVP намеренно не включает методы репрайсера или другие write-операции SalesFinder.
- Если коннектор станет многопользовательским, вынесите token cache и rate limiter в Redis.

## Особенность отчетов категорий

SalesFinder сначала создает отчет и возвращает `reportId`. После этого отдельные методы читают данные отчета. Формирование может быть не мгновенным, поэтому не объединяйте этот процесс в один длинный HTTP-запрос — Custom GPT Action лучше вызывает шаги отдельно.
