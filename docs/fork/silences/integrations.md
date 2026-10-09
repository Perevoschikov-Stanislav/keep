# Сайленсы: API интеграций и доставка lifecycle

Silence lifecycle доставляется транспортом `http-json-v1`. Адаптер принимает
события и отправляет их в настроенную систему. Mattermost не требуется.
`mattermost-api-v1` предназначен для incident notifications и не используется
как активный lifecycle subscriber.

## IaC

Пример с двумя независимыми HTTP receivers и двумя клиентами:
`config/silence-integrations.example/bundle.yaml`. Команды `cedar`/`quartz`
и пути групп — данные примера. В коде имён команд нет.

```text
AUTH_TYPE=OAUTH2PROXY
KEEP_TEAMS_CONFIG_FILE=/etc/keep/policies/team-policy.yaml
KEEP_SILENCES_INTEGRATIONS_CONFIG_FILE=/etc/keep/policies/bundle.yaml
```

Bundle соответствует общей схеме `keep.incidents/v1`. Указанный `access` должен
совпадать с действующим `TeamPolicy`; SHA256 проверяется по исходным байтам.
Проверяются schema, относительные pinned artifacts и ссылки. Значение tenant
сверяется с серверным single-tenant `keep`, а не выбирается из JSON запроса.
Значения secrets хранятся в env или смонтированных файлах. Отдельные service
tokens не должны совпадать с обычными admin API keys или друг с другом.
Зарегистрированный service token отклоняется общей API-аутентификацией, даже
если он также записан в БД как admin key. Header/Basic/Digest не обходят ceiling;
для него остаётся выделенный интеграционный API.

Из bundle используются `proof_profiles`, `service_clients`, `subscribers`,
`transports`, `destinations`, `dispatch`. Политики нормализации, корреляции,
workflows и доменное provisioning этим загрузчиком не применяются.
При наличии active managed configuration используется её snapshot. Работа с
ним описана в `../incident-core/provisioning.md`. Legacy file loader читает
настройки один раз при старте; изменение файла требует перезапуска API workers.
Невалидный legacy bundle останавливает startup; правила, аудит и outbox сохраняются.
Без переменной интеграционные клиенты отключены, worker продолжает учитывать
активацию/истечение обычных сайленсов. Без подписчиков новые доставки не создаются.

Для schema есть один файл в Python package;
`docs/fork/incident-core/contract-v1.schema.json` ссылается на него.
CLI `python3 lab/incident_contract.py validate ... --tenant keep` использует
тот же валидатор, что runtime.

Параметры, границы и defaults описаны в `../incident-core/parameters-v1.md`.
Credentials получателей перечитываются перед каждой отправкой. Они не входят
в событие, outbox payload, API диагностики или callback context.

## Operator proof

Запись требует одновременно `X-API-KEY` зарегистрированного клиента и
`X-Keep-Actor-Token` с пользовательским OIDC access token. Keep проверяет
подпись RS256/ES256, pinned issuer/JWKS, точный audience, интерактивный `azp`
или `client_id`, профиль claims, `sub`, `iat`/`exp`, `nbf` при наличии и свежесть.
Для audience разрешены заданная строка или массив только из этой строки.
`none`, HMAC, ID token, явные client-credentials/service-account markers и
неполные пути групп отклоняются. JWKS timeout/cache задаются в IaC;
неизвестный kid вызывает refresh с защитой от повторного fetch в течение секунды.

Роль вычисляется только из подписанных групп по тому же `TeamPolicy`, что UI.
Service scopes и команды ограничивают даже оператора с ролью admin.
Viewer не получает запись; responder пишет только в свои команды.
`null`/unassigned требует явного разрешения клиенту и настоящего admin.
READ_ONLY и проверки всех canonical targets сохраняются.

IaC issuer должен выдавать профиль, отличающий пользовательский access token
от ID/service token, и разрешать только интерактивные clients. В примере claim
`keep_actor_kind: user` требует настройки issuer; Keep его не выдумывает.
Service accounts/client-credentials grant у разрешённых интерактивных clients
должны быть выключены в IaC issuer. Прокидывание email/groups из callback или
канала не является proof. Если адаптер не может получить
user proof потребуется подтверждение в Keep.

Без proof — 401 `actor_proof_required`; неверный proof — 401
`invalid_actor_proof`; недоступный JWKS — 503 `actor_verification_unavailable`.
Нет прав — 403 или 404 для скрытого объекта. Legacy impersonation запрещена.
Даже при `LOG_AUTH_PAYLOAD=true` service token и actor token маскируются.

## API и восстановление

| Метод | Путь | Авторизация |
|---|---|---|
| GET | `/integrations/silences` | Service `read:silence`; snapshot с cursor |
| GET | `/integrations/silences/{id}` | Service `read:silence`; текущий ресурс |
| POST | `/integrations/silences/effective` | Service `read:silence`; Keep вычисляет coverage |
| GET | `/integrations/silences/deliveries` | Service `read:silence`; последние доставки в доступных командах |
| POST | `/integrations/silences` | Service `write:silence` + operator proof |
| PATCH | `/integrations/silences/{id}` | Service `update:silence` + operator proof |
| POST | `/integrations/silences/{id}/cancel` | Service `update:silence` + operator proof |

JSON команд и ответы совпадают с v1 `/silences`. Сервис БД общий. `origin`
берётся из регистрации клиента, actor — из проверенных issuer/sub. Namespace
квитанции включает клиента и стабильного оператора: refresh JWT его не меняет.
Повтор с тем же request UUID и содержимым возвращает исходный ответ;
другое содержимое — 409 `idempotency_conflict`. Повторный update/cancel не создаёт
дополнительный аудит/outbox. `expected_revision` защищает от конкурирующей записи.
Удалённая цель не мешает чтению/отмене существующего правила и replay разрешённой
команды. Keep отличает реально отсутствующую цель от существующего скрытого
объекта; перемещение цели в чужую команду не позволяет обойти проверку.

Snapshot ограничен `dispatch.snapshot_page_size`; следующая страница берётся
по `next_cursor` с тем же клиентом/фильтрами. Это страницы актуального реестра,
а не журнал с общей sequence или замороженный экспорт на время обхода.
После пропуска событий consumer перечитывает list/get и вызывает effective для
конкретных целей. Effective проверяет все цели вместе; частичный ответ при
запрещённой цели не выдаётся. Даты всегда вычисляются на момент запроса/отправки,
а не по сохранённому `event.state`.

## Outbox и часы

Миграция `9d6a8f3b5e27` после `8c5f7e2a4d16` добавляет
`notificationdelivery` и `notificationtransportbudget`. Изменение правила,
квитанция команды, immutable `SilenceEvent` и отдельные delivery rows каждого
подписчика фиксируются одной транзакцией. HTTP отправки внутри неё нет.

Worker запускается с API lifespan и может работать в нескольких процессах:
DB row locks/CAS сериализуют часы и команды, отдельный lease защищает доставку.
Запись берётся непосредственно перед отправкой; весь batch заранее не арендуется.
Ownership/lease проверяется и продлевается перед HTTP. Rate budget общий в БД.
Сбой receiver оставляет retry и безопасный error code; другой receiver имеет
свою запись. Redirect не выполняется и не переносит credentials на другой URL.
Служебные события не проходят notification gate и не схлопываются debounce.

Активация/истечение увеличивают revision. `effective_at` — точный starts_at/ends_at,
`occurred_at` — момент фиксации. Timer actor/origin — `keep-system`.
Если worker пропустил всё окно, выходит только expired. Cancel/update и timer
блокируют одну запись; устаревшая команда получает 409, устаревший timer не
публикует дополнительное событие. GET/effective корректны до работы timer.

Доставка допускает дубли после crash/потери ответа. `X-Keep-Event-ID`,
`X-Keep-Delivery-ID`, event ID/revision/origin/correlation сохраняются при retry.
Consumer хранит последнюю revision, игнорирует более старые/повторные snapshots,
восстанавливается через API и не превращает lifecycle event в новую команду.
Lifecycle JSON сам по себе не проходит command DTO. Correlation не заменяет
request UUID и deduplication. Изготовленная consumer новая команда с новым UUID
потребует действующего operator proof; семантически распознать такой echo
автоматически Keep не может.

После исчерпания retry запись остаётся `failed`; отключённая/переназначенная
подписка становится `disabled`. Диагностика возвращает ID, состояние, attempts,
даты и error code; receiver response/body/credentials не возвращаются.
Автоматического удаления истории и replay обычных уведомлений при unsilence нет.
Downgrade новых таблиц запрещён при наличии delivery history; rollback приложения
сохраняет эти дополнительные таблицы.
