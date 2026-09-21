# Довідники служб доставки (JSON)

Дані вивантажуються частинами (пейджинг по 5000 записів на файл).  
Повні базові зліпки нумеруються як `{назва}_1.json`, `{назва}_2.json` тощо.  
Щоденні файли змін (дельти) містять дату у форматі `YYMMDD`: `{назва}_YYMMDD_1.json`.

---

## 1. Нова Пошта (`data/np/`)

### Базові повні файли (Full Baseline)
- **Міста**: `city_1.json`, `city_2.json`, ...
- **Населені пункти**: `settlements_1.json`, `settlements_2.json`, ...
- **Відділення**: `warehouses_1.json`, `warehouses_2.json`, ...
- **Області**: `regions_1.json`
- **Райони**: `districts_1.json`

### Щоденні файли змін (Daily Deltas)
Містять лише додані, змінені або видалені записи за відповідну дату:
- `city_YYMMDD_1.json`
- `settlements_YYMMDD_1.json`
- `warehouses_YYMMDD_1.json`
- `regions_YYMMDD_1.json`
- `districts_YYMMDD_1.json`

---

## 2. Укрпошта (`data/up/`)

### Базові повні файли (Full Baseline)
- **Міста / населені пункти**: `city_1.json`, `city_2.json`, ...
- **Поштові відділення / індекси**: `postoffices_1.json`, `postoffices_2.json`, ...
- **Області**: `regions_1.json`
- **Райони**: `districts_1.json`

### Щоденні файли змін (Daily Deltas)
Містять лише додані, змінені або видалені записи за відповідну дату:
- `city_YYMMDD_1.json`
- `postoffices_YYMMDD_1.json`
- `regions_YYMMDD_1.json`
- `districts_YYMMDD_1.json`

---

> [!NOTE]
> Видалені записи позначаються полем `"_deleted": true` та ідентифікатором `"Ref"` (або відповідним ID).
