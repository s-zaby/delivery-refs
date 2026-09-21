# Інструкція зі встановлення та роботи зі скриптами

## Призначення

Скрипти призначені для щоденної автоматичної синхронізації та вивантаження довідкових даних поштових операторів (**Нова Пошта** та **Укрпошта**):
- Завантажують актуальні дані через API (області, райони, населені пункти, відділення).
- Зберігають актуальний стан у локальній базі даних SQLite (`data/delivery.sqlite3`) та виявляють зміни (додані, оновлені або видалені записи).
- Експортують дані у JSON-файли частинами (по 5000 записів за замовчуванням):
  - Базові зліпки: `{resource}_1.json`, `{resource}_2.json` тощо.
  - Щоденні зміни (дельти): `{resource}_YYMMDD_1.json` тощо.
- **Автоматична публікація в GitHub**: після кожного вивантаження згенеровані JSON-файли автоматично додаються в Git (`git add`), фіксуються коммітом (`git commit`) та відправляються у віддалений репозиторій (`git push`). Це дозволяє іншим сервісам та обробкам завантажувати найсвіжіші JSON-довідники безпосередньо з GitHub або GitHub Raw API.

---

## Встановлення

1. **Клонуйте репозиторій**:
   ```bash
   git clone https://github.com/s-zaby/delivery-refs.git
   cd delivery-refs
   ```

2. **Налаштуйте віртуальне середовище Python**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Створіть файл `.env` та вкажіть API ключі**:
   ```bash
   cp .env.example .env
   ```
   Відкрийте `.env` і додайте ключ(і):
   ```env
   NOVA_POSHTA_API_KEY=ваш_ключ_нова_пошта
   UKRPOSHTA_API_TOKEN=ваш_токен_укрпошта
   ```

4. **Підключіть GitHub репозиторій (для авто-пушу)**:
   Переконайтеся, що налаштовано `remote` та SSH/PAT доступ, щоб `git push` міг відправляти дані без інтерактивного вводу пароля:
   ```bash
   git remote -v
   # якщо remote ще не додано:
   git remote add origin git@github.com:s-zaby/delivery-refs.git
   ```

---

## Запуск вручну

### 1. Синхронізація Нової Пошти
```bash
source .venv/bin/activate
python np_sync.py
```
- Запуск для конкретної дати:
  ```bash
  python np_sync.py --date 2026-09-21
  ```
- Для повторної генерації повних JSON-файлів з локальної БД (без звернення до API):
  ```bash
  python np_sync.py --from-db
  ```

### 2. Синхронізація Укрпошти
```bash
source .venv/bin/activate
python up_sync.py
```
- Запуск для конкретної дати:
  ```bash
  python up_sync.py --date 2026-09-21
  ```
- Генерація повних файлів з локальної БД:
  ```bash
  python up_sync.py --from-db
  ```

---

## Налаштування запуску в Cron (кожної опівночі)

Щоб синхронізація запускалась автоматично щоночі о **00:00 (опівночі)**:

1. Відкрийте редактор розкладу cron:
   ```bash
   crontab -e
   ```

2. Додайте рядок (вкажіть абсолютний шлях до вашого проєкту замість `~/delivery`):

   **Тільки Нова Пошта:**
   ```cron
   0 0 * * * cd ~/delivery && ~/delivery/.venv/bin/python np_sync.py >> ~/delivery/data/np-sync.log 2>&1
   ```

   **Послідовний запуск Нової Пошти та Укрпошти:**
   ```cron
   0 0 * * * cd ~/delivery && ~/delivery/.venv/bin/python np_sync.py >> ~/delivery/data/sync.log 2>&1 && ~/delivery/.venv/bin/python up_sync.py >> ~/delivery/data/sync.log 2>&1
   ```

> [!TIP]
> Переконайтеся, що SSH-ключ користувача додано до GitHub (або налаштовано `credential.helper`), щоб cron міг виконувати `git push` у фоновому режимі без очікування пароля.
