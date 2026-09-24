# Пошаговый запуск на Windows

Все команды — в **PowerShell** (Пуск → «PowerShell»). Строки, начинающиеся с `#`, — комментарии.

## Шаг 0. Что понадобится
* Windows 10/11, интернет с доступом к `e-disclosure.ru` и `iss.moex.com` (e-disclosure отдаёт 403 не с российских IP).
* Python 3.11 или 3.12: https://www.python.org/downloads/windows/ — при установке поставьте галочку
  **«Add python.exe to PATH»**.
* Git for Windows: https://git-scm.com/download/win (можно без git — скачать ZIP ветки с GitHub и распаковать).

## Шаг 1. Получить код
```powershell
cd $HOME\Documents
git clone -b claude/insider-trading-strategy-77ou45 https://github.com/iamful-code/interfax.git
cd interfax
```

## Шаг 2. Виртуальное окружение и установка
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # если ошибка про политику выполнения -- см. «Проблемы»
python -m pip install --upgrade pip
pip install -e ".[dev]"
```
В начале строки приглашения должно появиться `(.venv)`. Активировать окружение нужно в каждом новом окне PowerShell.

## Шаг 3. Кодировка консоли (один раз в каждом окне)
Логи и отчёты на русском; чтобы консоль их не портила:
```powershell
chcp 65001 | Out-Null
$env:PYTHONUTF8 = "1"
```

## Шаг 4. Проверка, что всё работает (без сети)
```powershell
python -m pytest -q                 # ожидается: 97 passed
disclosure-alpha demo               # сквозной прогон на синтетических данных
```
Результаты demo: `data\reports\demo\event_study.md`, `backtest.md`, `car_paths.png`, `equity.png`.

## Шаг 5. Настройки (по желанию)
```powershell
$env:DA_DATA_DIR = "D:\disclosure_data"        # куда складывать данные (по умолчанию .\data)
$env:DA_EDISCLOSURE_MIN_INTERVAL = "2"          # пауза между запросами к e-disclosure, сек (по умолчанию 1.5)
$env:HTTPS_PROXY = "http://user:pass@host:port" # только если нужен прокси с российским IP
```
Переменные действуют до закрытия окна. Чтобы задать навсегда: Параметры → Система → О системе →
Дополнительные параметры системы → Переменные среды.

## Шаг 6. Разведка сайта — обязательно первым
```powershell
disclosure-alpha discover
```
Команда скачивает страницу поиска, ленту, одно сообщение и одну компанию, сохраняет HTML в
`data\discovery\` и сводку в `data\discovery\discovery.json`. Что проверить в выводе:
* `search: ok`, `lastnews: ok`, `event: ok`, `company: ok` — доступ есть. Если `error: HTTP 403` — гео-блок,
  нужен российский IP / прокси (шаг 5).
* В `discovery.json` → `pages.search.form.fields` должны быть поля `dateStart`, `dateFinish`, `lastPageNumber`,
  `lastPageSize`, а в `checkbox_groups` — группа типов сообщений (`eventTypeCheckboxGroup`). Если имена другие:
  ```powershell
  copy config\edisclosure_form.example.json config\edisclosure_form.json
  notepad config\edisclosure_form.json      # впишите реальные имена полей
  ```
* Если формы нет вообще (`form` отсутствует, сообщение «форма поиска не найдена») — страница строится
  скриптом; пришлите `discovery.json` разработчику, потребуется правка клиента.

## Шаг 7. Сбор данных (долгие шаги — можно оставлять на ночь, всё дозагружается инкрементально)
```powershell
# 7.1 Справочник акций MOEX + id компаний на e-disclosure по ИНН (~10-20 мин)
disclosure-alpha companies

# 7.2 Сообщения об изменении доли инсайдеров по всему рынку (1-3 часа за 10 лет)
disclosure-alpha messages --from 2015-01-01 --till 2026-09-01 --categories insider_stake_change --fetch-company-info

# 7.3 (для второй гипотезы) все сообщения эмитентов MOEX, по компаниям (несколько часов)
disclosure-alpha messages --from 2015-01-01 --till 2026-09-01 --mapped-companies --chunk-days 366

# 7.4 Тексты сообщений -> сделки инсайдеров -> таблица событий
disclosure-alpha events --categories insider_stake_change

# 7.5 Котировки MOEX ISS и индекс (~30 мин)
disclosure-alpha prices --from 2014-01-01 --till 2026-09-24
```
Если процесс прервался — просто запустите ту же команду ещё раз: уже скачанное берётся из кеша
(`data\cache\`), а сообщения/цены дописываются.

Чтобы сначала проверить конвейер на маленьком куске, возьмите короткий период:
```powershell
disclosure-alpha messages --from 2024-01-01 --till 2024-03-31 --categories insider_stake_change --fetch-company-info
disclosure-alpha events --categories insider_stake_change
disclosure-alpha prices --from 2023-01-01 --till 2024-06-30
```

## Шаг 8. Анализ
```powershell
disclosure-alpha study                                        # event study + скрининг категорий
disclosure-alpha backtest --hold-days 20 --split-date 2022-01-01   # стратегия, плацебо, walk-forward
```
Результаты — в `data\reports\`: `event_study.md`, `screening.csv`, `summary.csv`, `car_paths.png`,
`backtest.md`, `equity.png`, `trades.csv`. Открыть markdown удобно в VS Code или любом просмотрщике.
Варианты: `--hold-days 40`, `--direction -1` (продажи), `--categories buyback` (другая категория),
`study --model market_adjusted`.

## Проблемы и решения
| Симптом | Что делать |
|---|---|
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, затем повторить активацию; либо использовать `cmd` и `.venv\Scripts\activate.bat` |
| `disclosure-alpha : The term ... is not recognized` | Не активировано окружение (`.venv\Scripts\Activate.ps1`) или установка не прошла (`pip install -e ".[dev]"`) |
| `HTTP 403` от e-disclosure | Гео-блокировка: запускать из РФ или через прокси (`$env:HTTPS_PROXY`) |
| `заглушка антибот-защиты (HTTP 200, но контента нет)` | Проверка браузера не пройдена. Самое простое решение — режим `--browser` (см. раздел выше). Вариант с ручным копированием cookies — раздел «Cookies браузера» |
| `HTTP 503` с HTML-страницей (в `discover` все страницы `error: HTTP 503`) | Либо техработы, либо антибот-защита. 1) Откройте https://e-disclosure.ru/poisk-po-soobshheniyam в браузере: если не открывается — техработы, повторите позже. 2) Если открывается — это защита от ботов: см. раздел «Cookies браузера» ниже. `discover` сохраняет страницу ошибки в `data\discovery\search_page_error.html` и печатает вердикт (`! search: HTTP 503; признаки: ...`) — пришлите его разработчику, если шаги ниже не помогли |
| `HTTP 429` / соединения рвутся | Сайт ограничивает частоту: увеличьте `DA_EDISCLOSURE_MIN_INTERVAL` до 3-5 и запустите снова — прогресс сохранён |
| Кракозябры в консоли | Шаг 3 (`chcp 65001`, `PYTHONUTF8=1`) |
| `сообщений нет -- сначала выполните сбор` | Не выполнен шаг 7.2 или он ничего не нашёл — проверьте `discovery.json` и `config\edisclosure_form.json` |
| В `events.parquet` мало строк | Компании не сопоставились с MOEX по ИНН: посмотрите `data\processed\companies.parquet` (ИНН должен быть заполнен) и `mapping.parquet` |

## Cookies браузера (обход проверки браузера)

Сайт закрыт защитой, которая сначала отдаёт страницу-заглушку со скриптом: скрипт проверяет браузер,
выставляет cookies (`spjs`, `spsc`, `spid`) и только потом загружает настоящую страницу. Скрипту такую
проверку не пройти, поэтому cookies нужно взять из браузера. `discover` теперь распознаёт заглушку и
пишет `заглушка антибот-защиты (HTTP 200, но контента нет)`.

1. Откройте https://e-disclosure.ru/poisk-po-soobshheniyam в Chrome или Edge и дождитесь,
   пока вместо спиннера появится обычная страница с полями поиска.
2. Нажмите F12, вкладка **Network**, обновите страницу (F5), кликните самый первый запрос
   (`poisk-po-soobshheniyam`), справа откройте **Headers** → **Request Headers**.
3. Создайте файл `config\cookies.txt` по образцу `config\cookies.example.txt` и впишите в него две строки:
   * `User-Agent: ` и значение заголовка `user-agent` из браузера;
   * значение заголовка `cookie` целиком, одной строкой.
   ```powershell
   copy config\cookies.example.txt config\cookies.txt
   notepad config\cookies.txt
   ```
   Подойдёт и файл в формате Netscape из расширения «Get cookies.txt LOCALLY» — тогда строку `User-Agent:`
   допишите в начало файла вручную.
4. Повторите разведку:
   ```powershell
   disclosure-alpha discover
   ```
   В выводе должно появиться `cookies загружено: N (User-Agent взят из файла)` и `search: ok`.

Файл `config\cookies.txt` в git не попадает (он в `.gitignore`), потому что содержит данные вашей сессии.
Cookies проверки живут ограниченное время; когда вернётся заглушка, повторите шаги 1–3.
Строки-комментарии (начинаются с `#`) в файле можно оставить: скрипт их пропускает. Переносы строк внутри
длинных значений он убирает сам, так что вставка «как скопировалось» допустима. Вывод `имена cookies: ...`
показывает, что именно распозналось: там должны быть `spjs`, `spsc`, `spid` и `.AspNetCore.Antiforgery...`.

Если `discover` сообщает, что отвечает только хост с `www.`, задайте
`$env:DA_EDISCLOSURE_BASE_URL = "https://www.e-disclosure.ru"`.

## Режим браузера (рекомендуется)

Сайт закрыт проверкой браузера, и cookies после неё живут недолго. Надёжнее не копировать их вручную,
а ходить настоящим Chromium: он проходит проверку сам, а дальше запросы идут через его же сетевой стек.

Установка (один раз):
```powershell
pip install playwright
playwright install chromium
```

Использование: добавьте `--browser` к сетевым командам.
```powershell
disclosure-alpha discover --browser
disclosure-alpha companies --browser
disclosure-alpha messages --from 2024-01-01 --till 2024-03-31 --categories insider_stake_change --fetch-company-info --browser
disclosure-alpha events --categories insider_stake_change --browser
```
Чтобы режим включался всегда, задайте `$env:DA_BROWSER = "1"`.

Полезные детали:
* Профиль браузера хранится в `data\browser_profile\`, поэтому пройденная проверка переживает перезапуск команды.
* Если проверка требует ручного действия (капча), запустите с видимым окном: `--show-browser`,
  пройдите проверку в открывшемся окне, дальше команда продолжит сама.
* Окно не нужно держать открытым: команда закрывает браузер по завершении.
* Как это работает: страницы открываются настоящим переходом (как если бы вы кликнули по ссылке), а отправка
  формы поиска выполняется запросом изнутри уже открытой страницы. Защита отличает такие обращения от
  программных, поэтому «в лоб» через обычный HTTP-клиент они не проходят.
* Скорость: сбор через браузер медленнее обычных запросов, но кеш (`data\cache\`) работает так же,
  поэтому повторные прогоны почти бесплатны. Для массового сбора можно попробовать ускорение
  `$env:DA_BROWSER_NAVIGATE_GET = "0"` (страницы запрашиваются без перехода); если вернётся заглушка,
  уберите эту переменную.

## Чего не делать
Не копируйте скачанные страницы сайта в репозиторий и не пересылайте их: в заглушке защиты лежит
обфусцированный сторонний скрипт. Каталоги `data\discovery\` и `tests\fixtures\live\` добавлены
в `.gitignore`. Если понадобится показать разметку, присылайте небольшой фрагмент нужного блока
(например, HTML формы поиска), а не файл целиком.
