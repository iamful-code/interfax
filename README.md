# disclosure-alpha — раскрытия e-disclosure.ru как торговый сигнал

Проверка двух гипотез на данных Центра раскрытия корпоративной информации (e-disclosure.ru) и котировок MOEX:

1. **H1.** Раскрытые сделки менеджмента и совета директоров (сообщения «Об изменении размера доли участия
   лица, входящего в состав органов управления эмитента») предсказывают будущую доходность акций, и на этом
   можно построить стратегию.
2. **H2.** Среди остальных типов сообщений (buyback, дивиденды, крупные акционеры, «иные существенные
   события», рейтинги, листинг…) есть информативные — их отбирает автоматический скрининг.

Подробно: [docs/research_plan.md](docs/research_plan.md) (гипотезы, методология, критерии) и
[docs/site_structure.md](docs/site_structure.md) (что известно о сайте, что нужно подтвердить).

## Статус

| Часть | Состояние |
|---|---|
| Скрапер e-disclosure: `discover`, поиск с пагинацией, лента, страницы событий/компаний (ИНН) | написан, покрыт тестами на HTML-фикстурах; **разметка сайта не подтверждена вживую** (см. «Сеть») |
| Разбор сообщений об изменении доли (ФИО, должность, доля до/после, дата, направление, роль) | написан, тесты на форматах 454-П и 714-П |
| Таксономия 25 категорий сообщений (`config/event_types.yaml`) | готова |
| Загрузчик MOEX ISS (справочник с ИНН, дневные свечи, IMOEX, инкрементальный кеш) и маппинг ИНН→тикер | готов, тесты на JSON-фикстурах ISS |
| Event study (CAR/BHAR, рыночная модель, бутстрап, портфель в календарном времени, скрининг с BH-FDR) | готов, тесты на синтетике с известным эффектом |
| Бэктест (издержки, лимит участия в обороте, walk-forward, плацебо) и отчёты | готов |
| Сквозной прогон `demo` на синтетике с известным эффектом | проходит: скрининг находит эффект только там, где он вшит ([docs/demo](docs/demo/README.md)) |
| Режим сбора через настоящий браузер (Playwright + Chromium) | готов: сам проходит JS-проверку сайта, проверен на локальном стенде с такой же проверкой |
| Прямой сбор через поиск сайта (`api/search/sevents`) и справочник типов (`api/data/sevent-types`) | готов: устройство запроса подтверждено на живых данных, разбор результатов проверен (100 сообщений за запрос) |
| **Живые данные** | не собраны. Сайт закрыт проверкой браузера: без cookies из браузера отдаётся страница-заглушка (`discover` это распознаёт). Порядок обхода -- в [docs/windows_quickstart.md](docs/windows_quickstart.md), раздел «Cookies браузера» |

## Сеть — важно прочитать

* Окружение, где писался код, блокирует все хосты, кроме GitHub/PyPI. Запускать сбор нужно с машины,
  откуда доступны `https://e-disclosure.ru` и `https://iss.moex.com`.
* По данным открытых проектов, **e-disclosure.ru отвечает 403 не с российских IP**, а при частых запросах
  временно блокирует. Клиент держит паузу ≥1.5 с между запросами (`DA_EDISCLOSURE_MIN_INTERVAL`) и кеширует
  ответы на диск, так что повторные прогоны бесплатны.
* Первым делом выполните `disclosure-alpha discover`: он сохранит HTML ключевых страниц в `data/discovery/`
  и `discovery.json` с найденными полями формы поиска. Если имена полей отличаются от предположенных
  (`dateStart`, `dateFinish`, `lastPageNumber`, `lastPageSize`, `eventTypeCheckboxGroup`, `query`, `queryId`),
  пропишите их в `config/edisclosure_form.json`:
  ```json
  {"field_map": {"date_from": "dateStart", "date_till": "dateFinish", "page": "lastPageNumber",
                 "page_size": "lastPageSize", "event_types": "eventTypeCheckboxGroup"}}
  ```
  Код менять не нужно.

## Установка

Пошаговая инструкция для Windows (PowerShell): [docs/windows_quickstart.md](docs/windows_quickstart.md).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,browser]"   # browser -- режим обхода JS-проверки сайта
playwright install chromium  # только для режима --browser
python -m pytest -q          # все тесты офлайн
disclosure-alpha demo        # сквозной прогон на синтетических данных -> data/reports/demo/
```

Пример вывода `demo` (отчёты и графики) лежит в [docs/demo/](docs/demo/README.md).

## Порядок запуска на живых данных

```bash
disclosure-alpha discover --browser                          # 1. структура сайта (--browser проходит JS-проверку сайта)
disclosure-alpha companies                                  # 2. справочник акций MOEX (ISS) + id компаний e-disclosure по ИНН
disclosure-alpha messages --from 2015-01-01 --till 2026-09-01 \
    --categories insider_stake_change --fetch-company-info  # 3a. сообщения об изменении доли по всему рынку
disclosure-alpha messages --from 2015-01-01 --till 2026-09-01 \
    --mapped-companies --chunk-days 366                     # 3b. (H2) все сообщения эмитентов MOEX, по компаниям
disclosure-alpha events --categories insider_stake_change   # 4. тексты сообщений -> сделки инсайдеров -> events.parquet
disclosure-alpha prices --from 2014-01-01 --till 2026-09-24  # 5. свечи ISS + IMOEX
disclosure-alpha study                                       # 6. event study + скрининг категорий -> data/reports/
disclosure-alpha backtest --hold-days 20 --split-date 2022-01-01   # 7. стратегия, walk-forward, плацебо
```

Все команды инкрементальны: повторный запуск дозагружает только новое. Переменная `DA_DATA_DIR` задаёт
каталог данных (по умолчанию `./data`, в git не попадает).

## Что получается на выходе

* `data/processed/messages.parquet` — строки поиска (id, компания, время публикации, тип).
* `data/processed/companies.parquet`, `mapping.parquet` — ИНН/ОГРН компаний и связь ИНН → тикер (первичная
  обыкновенная акция помечена `primary`).
* `data/processed/insider_trades.parquet` — разобранные сделки: ФИО, должность, роль (ceo / board_chair / board /
  management_board), доля до/после, Δ п.п., дата, направление, признак подконтрольной организации.
* `data/processed/events.parquet` — вход для анализа: `event_id, secid, published_at, category, direction, size`,
  флаг `has_confounder` (рядом другие ценочувствительные сообщения).
* `data/reports/`: `event_study.md` (диагностика, скрининг категорий с q-value, сводка CAR по окнам),
  `car_paths.png` (средний CAR −20…+60 дней по категориям/направлениям), `backtest.md`, `equity.png`,
  `trades.csv`, `equity.csv`.

## Структура

```
config/event_types.yaml          категории сообщений (регулярные выражения, правило направления)
src/disclosure_alpha/
  http.py                        HTTP-клиент: лимит частоты, повторы, дисковый кеш
  edisclosure/client.py          discover / поиск / лента / событие / компания
  edisclosure/parsers.py         разбор HTML (устойчив к вёрстке: ссылки EventId / company id, даты)
  edisclosure/insider.py         разбор «изменение доли»: доли до/после, направление, роль
  edisclosure/taxonomy.py        классификация заголовков по категориям
  moex/iss.py, prices.py, mapping.py   ISS, хранилище цен, ИНН -> тикер
  analysis/timing.py             день события t0 (отсечка 18:40 МСК), день входа
  analysis/event_study.py        CAR/BHAR, статистика, скрининг, портфель в календарном времени
  analysis/backtest.py           бэктест, walk-forward, плацебо
  analysis/report.py             markdown-отчёты и графики
  pipeline.py, cli.py, synthetic.py
tests/                            фикстуры HTML/JSON и тесты (офлайн)
```

## Ограничения и следующие шаги

* Разметка e-disclosure восстановлена по открытым источникам — после `discover` возможно потребуется поправить
  `config/edisclosure_form.json`; парсеры результатов от вёрстки почти не зависят.
* История ISS не скорректирована на дивиденды (для окон ≤60 дней смещение мало); TODO: `/iss/securities/{secid}/dividends.json`.
* Для H2 направление большинства категорий требует чтения текста (сумма дивидендов, «повышен/понижен» рейтинг):
  скрининг сначала отбирает информативные категории по |CAR| и дрейфу, затем для них пишется парсер направления.
* Альтернативный источник для валидации H1 с 2025 г. — «Показатель торговой активности инсайдеров» Мосбиржи.
