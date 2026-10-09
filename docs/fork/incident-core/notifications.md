# уведомления, маршруты и команды интеграции

## IaC и проекция

`routes[]` — managed resources общего validate → preview → apply конфигурации.
Маршрут выбирается по canonical team, event type, CEL и наибольшему приоритету.
Равный приоритет в пересекающейся team/event области отклоняется до apply.
Неизвестная команда/нет совпадения — `no_route`, без чужого fallback.
Fan-out и contacts ограничены canonical командой.

Настройки: event types, severity/CEL, presentation, contacts, destination references,
append/upsert и явные fallbacks. Timeout, capped retry/backoff, общий rate budget
и debounce принадлежат transport; cadence/batch/lease — dispatch. CEL компилируется
до apply. Credentials задаются references и разрешаются перед HTTP, не хранятся
в DTO/snapshot/receipts и не выводятся в диагностике.

`config/incident-notifications.example/a,b` используют разные произвольные команды,
timings, контакты и действия. `a` — только HTTP JSON; `b` — Mattermost API + HTTP JSON.
Channel ID — options destination, он не определяет grouping/ownership/roles.
Workflow `record-example` остаётся mock, реальные серверы примерами не настраиваются.

`Notification` v1 содержит canonical incident ID, lifecycle/assignment
`incident_revision`, episode, event/delivery IDs, team, policy digest, готовые
title/description/fields/links/actions и contact/destination/transport references.
Начальная canonical revision — **0**, как в доменном сервисе lifecycle.
`projection_revision` начинается с 1 и растёт при изменении представления: это
версия карточки для receipts/out-of-order, отдельно от command CAS.
Неизвестное display value — null. Common DTO не содержит channel/post IDs.

Перед отправкой содержимое пересобирается из canonical DB state и активного IaC.
Ручные title/summary имеют приоритет. UI/notifications используют общие разрешённые
template values: normalized fields, incident ID/status/severity, assignee/count,
episode/revision и публичные flap/SLA fields. Raw events, credentials и произвольные
enrichments не доступны шаблонам.

## Профили

| Профиль | Update | Buttons | Receipts | Отправка |
| --- | --- | --- | --- | --- |
| `http-json-v1` | Нет | Нет | Нет | Ready DTO через POST на endpoint + path |
| `mattermost-api-v1` | Да | Нет | Да | POST/PUT поста, renderer существующего provider |
| `mattermost-bridge-v1` | Да | Нет | Да | Готовый envelope через отдельный transport bridge |

Capabilities точно соответствуют реализованному профилю. HTTP upsert требует
`update_fallback: append`; actions — `actions_fallback: keep_link`. Неподдержанный
profile/capability не активируется. Действия ведут к login/подтверждению в Keep.
Адаптеры не группируют инциденты, не считают SLA и не назначают права по каналу.
TLS проверяется, redirects запрещены, receipt response ограничен 64 KiB.
HTTP sender не пишет headers/body/response body в логи.

Mattermost token должен иметь доступ к настроенным каналам и редактированию постов.
Используются официальные API, без предположения об exactly-once:

```
https://docs.mattermost.com/api/reference/create-post
https://docs.mattermost.com/api/reference/update-post
https://docs.mattermost.com/api/reference/get-post
```

## Общая очередь, gates и сбои

Используются `NotificationDelivery` и общий worker: общие leases/rate budget/retry
для incident notifications и служебных silence events. Их DTO/gates различаются.
Новой transport queue, брокера или отдельного worker daemon нет.

`Incident.notification_context` сохраняет signature/sequence одновременно с outbox.
Bounded cursor и tenant/group/incident locks сериализуют сканы committed state.
Downtime coalesces промежуточные версии: это актуальные проекции, не журнал всех
переходов. Domain audit сохраняется независимо. Repeated scan/apply не создаёт
новую отправку. Quiet flap reset обновляет карточку без нового входящего события.

Перед send/retry проверяются текущий owner и полная linked fingerprint history,
visibility/status, route/destination fingerprint, canonical projection и silence.
Mixed ownership не отправляется. Full silence — skip; partial —
`silence_partial_payload_unsafe` для всего агрегата. Истечение silence не проигрывает историю. `after_silence: current_active`
разрешает отправить актуальное активное состояние после снятия последнего блокера.
Новое изменение/разрешённый due repeat использует текущее состояние.
Merge/delete/reassignment прекращают отложенную отправку старой цели;
уже принятые внешним API сообщения не отзываются.

Destination/reminder operations входят в ту же очередь. На send повторно
проверяются episode/chain/level/repeat/stop/contacts. ACK после enqueue отменяет
ожидающую эскалацию. Результат каждого назначения сохраняется в operation/audit.
Сайленс не останавливает ordinary remediation и business timers.

`IncidentNotificationBinding` хранит external ID/confirmed version и lease на
canonical incident/destination/transport. Endpoint/auth reference/options входят
в binding identity. Параллельные sender не обновляют один пост одновременно.
Старый token не записывает результат после reclaim.

- Lease истёк до effect start: safe reclaim с новым token.
- Connect timeout/refused/DNS failure до HTTP, отсутствующий credential, явный rejection: capped retry.
- Потеря ответа, 5xx, malformed receipt или crash после effect start: `unknown`;
  binding удерживается, новый create/update назначения не выполняется.
- Остальные назначения продолжают доставку. Уже начатый внешний эффект не отзывается.
- HTTP JSON без receipts требует отдельного рассмотрения unknown у receiver;
  автоматического небезопасного replay нет.

Receipt требует service scope `update:notification`, team ceiling и совпадение
с transport.callback_client_ref. Delivered receipt для Mattermost проверяется GET:
ID/channel и server-written notification/incident/projection markers. Только эта
проверка снимает unknown hold. Failed/unknown assertion не доказывает отсутствия
create. Старый receipt не снижает confirmed revision и не меняет новый binding.

## API и команды

| Endpoint | Назначение |
| --- | --- |
| `GET /integrations/notifications?cursor=<uuid>&limit=<n>` | Bounded canonical snapshot/gate в области service client |
| `GET /integrations/notifications/deliveries` | Scoped queue metadata без payload/credentials/proof |
| `GET /integrations/notifications/deliveries/{notification_id}` | Свежий gate/envelope только для зарегистрированного callback service client назначения |
| `POST /integrations/notifications/commands` | Strict IncidentCommand v1 + service update:incident + signed user proof |
| `POST /integrations/notifications/receipts` | Проверка подтверждения внешней доставки, без изменения domain status |
| `POST /incidents/{id}/commands` | Тот же доменный путь после подтверждения человека в Keep |

Silence create/update/cancel используют существующие `/integrations/silences` и
`/silences` v1. Lifecycle event не превращается обратно в команду.
Snapshot восстанавливает consumer projection; чтение само не создаёт отправку.
Если limit не указан, размер страницы берётся из dispatch.snapshot_page_size;
явный limit не может превышать этот IaC ceiling, общие границы — 1–1000.

Source key регистрируется в IaC. RS256/ES256 signature, issuer/audience/party,
claims/freshness/groups проверяются до команды. User scopes/member teams пересекаются
с service ceiling. Body/header/channel не назначают admin. Owner/history/scope
и active configuration снова проверяются на locked incident.
Legacy API key/impersonation не заменяет человека в confirmation endpoint.

Read/receipt bridge client имеет только `read:incident`, `read:silence`,
`update:notification`; ему разрешён пустой `proof_profile_refs`. Любой delegated
mutation scope требует непустых proof profiles. Мост не выполняет команды от
service admin; его action links ведут к человеческому подтверждению в Keep.

`proof_profiles[].username_claim: null` сохраняет principal `sub`. Для другого
Keep username задаётся, например, `preferred_username`; configured claim обязателен
и читается после signature verification. Actor identity/idempotency остаётся
**issuer + sub**. Receipt хранит verified actor и registered origin без токена.

CAS, status/assignee, alert enrichments, audit, outbox и command receipt коммитятся
вместе. Replay UUID/body возвращает receipt; другой body — conflict.
After-commit workflow/UI updates используют существующий IncidentBl путь.
Responder назначает себя/снимает назначение; admin — существующего Keep user
с update scope. В User table нет авторитетного membership третьего оператора,
поэтому responder не назначает произвольное чужое имя.

Открытие notification link ничего не меняет: требуется клик подтверждения.
Stale revision/read-only/foreign team блокируют действие; API повторяет проверки.
Silence link открывает существующий редактор для canonical incident.
Legacy `/incidents/{id}` перенаправляет на `/alerts` с query; login сохраняет весь
callback URL через URL.searchParams, поэтому command/revision доходят до подтверждения.

## Миграция и запуск проверки

Additive revision `a46c093ef271` после `6b2fda31c890`: nullable incident context,
legacy-compatible defaults shared delivery, bindings/cursor/command receipts.
Existing silence delivery и ручной incident state сохраняются. Downgrade с
notification state отказывается терять данные.

```
python3 lab/check-incident-notifications-k3d.py
```

Runner использует `k3d-local/keep-lab`, отдельный Job и loopback PostgreSQL sidecar,
не БД/Deployment основной лабы. Inputs/results/logs находятся в
`.lab-work/incident-notifications-k3d/`. Wire receivers/JWKS принадлежат fork.
Проверки внешнего адаптера выполняются отдельно через `lab/check-mm-bridge-k3d.py`.
