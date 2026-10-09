# IncidentPolicies contract v1

Машинный формат: `contract-v1.schema.json`; сообщения и сценарии: `fixtures-v1.json`.

## 2. Источники состояния

- Keep владеет tenant/team, исходными событиями, составом/ID/эпизодом инцидента,
  переходами, ACK/resolve/assignee, флаппингом, deadlines, сайленсами и аудитом.
- IaC владеет политиками, mappings/workflows, presentation, routing, contacts,
  зарегистрированными интеграциями и настройками доставки.
- Адаптер хранит external message/destination binding, последнюю подтверждённую
  ревизию и delivery result. Он не изменяет состав, SLA или состояние инцидента.
- Team/access group и correlation key различаются. Канал, адрес и transport kind
  не определяют владельца, разрешения или grouping key.

При недоступном транспорте Keep продолжает принимать события и обслуживать
инциденты. Наличие Mattermost-конфигурации не требуется.

## 3. Bundle и совместимость

Bundle — один YAML `api_version: keep.incidents/v1`, `kind: IncidentPolicies`,
`id`, `tenant_id`, `revision`, `keep_url`. `tenant_id` обязательно сверяется с
доверенным tenant deployment/apply; тело файла не выбирает произвольный tenant.

`access` указывает на **единственный действующий TeamPolicy v1**. Он содержит
role groups `admin/responder/viewer/noc`, team groups, zones, `visible_to` и
`incident_views`. Второго набора ролей или membership в bundle нет.
`visibility: all` меняет только существующие правила чтения; responder всё равно
пишет только в свои команды, viewer/noc не пишут. Нельзя отключить RBAC,
canonical team checks, проверку proof или аудит параметром политики.

`access`, `mappings[].artifact`, `extraction[].artifact`, `rules[].artifact` и
`workflows[].artifact` содержат относительный
`path` и SHA-256 **точных байтов** файла. Абсолютные пути, выход через `..`/symlink,
повторное использование файла под другим ресурсом, отсутствующий или изменённый
файл отклоняются. Проверенные байты разбираются один раз; файл не открывается
повторно после проверки digest. Повторные YAML keys/merge overrides, циклы,
не-JSON значения и неизвестные параметры отклоняются.

Существующие mapping поля сохранены, без нового DSL: `name`, `description`,
`file_name`, `priority`, `matchers`, `type`, `rows`, `is_multi_level`,
`new_property_name`, `prefix_to_remove`. CSV columns — данные, а не неизвестные
параметры схемы. Запись canonical tenant/team/role/actor/fingerprint запрещена;
`zone` должна относиться к команде того же TeamPolicy.
Mapping name — отображаемое имя, logical ID — `mappings[].id`.

Workflows сохраняют wrapper `workflow:`, triggers, steps/actions, provider `with`,
`notification` и existing secret references. `workflow.id` совпадает с logical ID.
Offline validator проверяет структуру и текущий pre-parser. Runtime compatibility
validator также проверяет сигнатуры provider methods, литералы
параметров и выражения без внешних вызовов/разрешения secrets. Подключение к
provider проверяется при выполнении. Это не новый workflow DSL.

ID ресурса scoped `(tenant, kind, logical_id)`. ID разных видов могут совпадать;
два ID одного вида — ошибка. Переименование файла не меняет идентичность ресурса.
Изменённый artifact path/digest меняет provenance версии. Никакого adoption
UI-объекта по совпавшему имени: `adoptions` требует tenant-scoped target ID и digest
проверенного target. Явные команды adoption/restore показаны в `provisioning.md`.

`revision` — человеческая метка. Digest вычисляется по bundle и проверенным
artifact documents; идентичное повторное применение — no-op. Defaults из schema
не подставляются offline validator. Runtime фиксирует effective defaults в
неизменяемом snapshot. Отсутствие optional feature означает `absent` из inventory;
отсутствие mount/file целиком никогда не является командой удаления.

## 4. Домен и приоритеты

Контекст CEL содержит canonical `team_id`, исходные `labels/source/severity`,
`normalized` и актуальное состояние инцидента в соответствующем этапе.
CEL не выбирает tenant/team и не заменяет проверку прав. Неизвестное raw поле
обрабатывается как missing; неверное выражение — ошибка проверки, не match-all.
Declared field types и runtime context проверяются при compatibility validation.

| Область | Определённое поведение |
| --- | --- |
| Normalization | Высший `priority`, первый matching rule в canonical team scope. `sources` — упорядоченные aliases, первый непустой источник; explicit owner/workload раньше regex из pod name. Regex failure даёт объявленный `missing`. Выход только в перечисленные normalized fields, с provenance и diagnostics |
| Presentation | Generated title/description, поля/подписи/порядок, цвета, ссылки и действия. Simple mustache field references; secrets не доступны. Ручной `user_summary`, notes и assignee не перезаписываются |
| Correlation | Tenant/team/rule добавляются core всегда. `group_by` — ordered typed tuple; `required_fields` включает каждый его путь. JSON types, missing, null, empty и delimiters не склеиваются в общий ключ. `separate_alert` либо `skip_correlation` сохраняют исходный алерт и diagnostics |
| Correlation overlap | `first_match` берёт высший priority; `parallel` создаёт независимые rule-scoped группы. Равный priority в пересекающемся team scope отклоняется, даже если автор считает CEL разными |
| Correlation window | `[group_started_at, group_started_at + window_seconds)`; на верхней границе новая группа. `threshold` считает разные canonical alerts, повтор версии не увеличивает число |
| Lifecycle | `resolve_on/create_on` используют существующие значения Rule. `incident.acknowledged` — событие ACK существующего `in_progress`, не параллельная система статусов |
| Reopen | В `reopen` тот же ID возвращается внутри окна; вне окна новый связанный ID. `new_incident` всегда новый ID. Новый ID всегда сбрасывает ACK/SLA и не наследует ID-selector silence. Перенос assignee задан отдельно, manual notes/history сохранены |
| Flapping | Distinct transitions в `(now-window, now]`; duplicate/repeated resolved не считается. Quiet reset на `last_transition + reset_after`. Late event хранится в history и не откатывает более новое состояние |
| Automation | От declared SLA origin считаются ACK deadline и возрастающие level offsets. Repeat limit включает первый запуск; `0` повторяет до stop/следующего уровня, `repeat_every_seconds: 0` запрещает повтор. Resolve всегда останавливает цепочку. ACK/resolve/revision перечитываются перед выполнением |
| Routing | Высший priority/`first_match` либо `fanout`; каждый canonical team выбирает только свои destination/contact candidates. Одинаковый destination дедуплицируется; разные назначения доставляются независимо. Нет маршрута — no delivery + diagnostic, без чужого fallback |

`team_ids: [a,b]` означает кандидатов для этих команд. При обработке объекта
выбираются destination/contact с **тем же canonical team**, а не все кандидаты
из списка. `team_id: null` в назначении и `null` в team scope выбирают только
unassigned; это не wildcard. Для такой записи оператору всё равно нужен admin.

Workload/PVC/node/service — примеры правил. Их имена, namespaces, aliases,
grouping keys, таймеры и шаблоны находятся в YAML; встроенных OPS/IT presets нет.

## 5. Validate → preview → apply

Normalization может ссылаться на presentation через `presentation_ref`
для отображения в Keep до routing. Sources допускают quoted dictionary keys
(`labels["app.kubernetes.io/name"]`). Поддержанные presentation paths и порядок
ingestion описаны в `normalization.md`. Runtime принимает presentation
actions ACK/resolve/assign/silence с подтверждением человека в Keep.

Runtime принимает `correlation`, first_match/parallel overlap и базовые
resolve policies. Typed tenant/team/rule/revision key, half-open fixed window,
distinct firing threshold, diagnostics и pinned explanations описаны в
`correlation.md`. `multi_level` и `multi_level_property_name` добавлены
в каноническую схему/каталог для dictionary expansion. Lifecycle реализует
event_time/receive_time, reopen/new_incident, ACK/assignee policy и flapping/reset.
Границы, epoch membership, canonical state/audit и pinned effects описаны в
`lifecycle.md`. Conditional expressions в create_on=all отвергаются.
Новые correlation и legacy rules выбираются раздельно. Automation:
`automation.md`; routing/notification DTO, общая очередь, recovery и
command CAS описаны в `notifications.md`.

1. Загрузить bundle и все pinned artifacts в candidate snapshot, проверить schema,
   current formats/CEL, references, capabilities, tenant/team и supported activation.
2. Preview относительно конкретного active digest: creates/changes/deletions,
   владение, воздействие на открытые группы/jobs, collisions и migration needs.
   Secrets/proof/token values не попадают в preview; ссылки и non-secret IDs допустимы.
3. Apply с expected active digest и digest проверенного candidate/preview. Изменение
   файлов/активной версии после preview — conflict, требуется новый preview.
4. Одна DB transaction публикует managed configuration и active snapshot.
   Применение по файлам с частичным успехом запрещено. Все workers используют
   одну active revision; worker с неподтверждённым snapshot не начинает новое действие.

Missing env/mount/file, invalid config или unsupported activation сохраняют
последний active snapshot. Пустой каталог не очищает БД. Legacy loaders: отсутствие входа сохраняет ресурсы, а ошибка последнего файла
откатывает общую startup транзакцию workflows/mappings.

Desired bundle содержит сохраняемые managed ресурсы. Omission ранее managed ID
без explicit `deletions` — ошибка preview/apply, с сохранением active snapshot.
Запись удаления содержит `kind`, `id`, `expected_resource_digest` и не может
одновременно присутствовать в desired resources. Matching предыдущего ресурса,
references/ownership/open-state checks выполняет provisioning. Пример:

```yaml
deletions:
  - kind: correlation
    id: retired-rule
    expected_resource_digest: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Removal существующей команды с принадлежащими ей данными требует reviewed
ownership migration; не превращает эти данные в unassigned автоматически.
Удаление managed policy не удаляет инциденты, историю или silence audit.

Rollback выбирает предыдущий сохранённый snapshot, проходит новый preview,
expected-active check и explicit deletion plan для лишних managed ресурсов.
Он не возвращает прошлые ACK/assignee/notes, delivery receipts или бизнес-эпизоды.
Policy-owned поля читаются в UI, запись через обычный UI/API отклоняется с source/version.

### Воздействие на уже открытое состояние

| Изменение | Эффект |
| --- | --- |
| Team membership / reading policy / client / proof profile | Следующая проверка доступа использует active policy; новые настройки не добавляют роли из callback context |
| Mapping / normalized identity / group_by | Только новые события/группы. История/открытые группы не перепривязываются без reviewed migration |
| Lifecycle / flap | Текущий эпизод pins policy snapshot; новые группы используют active revision |
| ACK deadlines / escalation levels | Текущий эпизод pins policy/выбранные references; следующий использует active revision. `on_policy_update: next_episode/reschedule/cancel` явно выбирает режим в preview; обычный повтор apply не создаёт вторую цепочку |
| Presentation / contact address / routing / transport | Следующая проекция/попытка учитывает active configuration и team scope; удалённый destination останавливает старую доставку с diagnostic |
| Outbox worker parameters | Reload worker; lease/ownership проверяются на каждой отправке, не меняют effective silence time |

У Job есть business key `(incident, episode, policy, level, destination)`.
Повтор apply/job не создаёт новый бизнес-переход. Worker повторно проверяет
canonical состояние; изменения конфигурации не оживляют завершённый timer.

## 6. Адаптеры и wire

Каталог v1 содержит следующие реализованные профили:

| kind / adapter_ref | Capabilities | Scoped options | События |
| --- | --- | --- | --- |
| `http_json` / `http-json-v1` | update/actions/receipts=false | `destination.options.path`, JSON POST к receiver | Silence lifecycle и incident notifications |
| `mattermost` / `mattermost-api-v1` | update/receipts=true, actions=false; ссылки с подтверждением в Keep | `destination.options.channel_id`, endpoint и bot auth_ref | Incident notifications |

Legacy MattermostProvider продолжает отправлять через incoming webhook. Новый
профиль использует renderer этого provider и общий HTTP sender: POST создаёт пост,
PUT обновляет binding, GET проверяет receipt. Настоящий mm-bridge ещё не переведён.
Регистрация другого поддержанного адаптера расширяет каталог/его schema, без
изменения normalization, grouping, access, silences или SLA.

`upsert` без update требует `update_fallback: append`; отсутствие buttons —
`actions_fallback: keep_link`. `reject` в несовместимой конфигурации — ошибка.
Fallback ведёт в Keep с login/confirmation и canonical role/team checks.

`Notification` содержит notification/event UUID, tenant/team, canonical incident
UUID/revision/episode, policy digest, event type, ready title/description/fields,
rendered links/actions, contact refs, transport/destination refs и correlation.
Нет обязательных channel/post IDs, attachments, `mm_members`, flaps или timers.
Одна проекция на destination соответствует одному canonical incident.
`incident_revision` — lifecycle/assignment CAS, начальное значение 0;
`projection_revision` — версия отображения, начиная с 1. Смена представления
не выдаёт транспорту право выполнить команду с устаревшей canonical revision.

`IncidentCommand` задаёт request UUID, incident UUID, expected revision,
ack/resolve/assign и correlation; assign отдельно содержит assignee.
`actor/role/groups/origin/tenant_id` в body запрещены. Подлинный источник callback
проверяет адаптер; Keep всё равно требует service identity и operator proof.
Silence create/update/cancel не получают нового wrapper: используются **точные**
v1 schemas и правила `../silences/contract-v1.md` на `/integrations/silences`.

`DeliveryReceipt` — delivered/failed/unknown, optional opaque external ID и
подтверждённая revision. Timeout после внешнего create означает unknown, не success.
Keep хранит binding/confirmed revision и блокирует неопределённую попытку назначения.
Delivered receipt для Mattermost проверяется внешним GET и совпадением post/channel,
notification/incident/projection markers. HTTP JSON без receipts не повторяет
unknown автоматически. Canonical snapshot восстанавливает consumer projection.
Exactly-once доставки не обещается.

## 7. Proof, outbox и сайленсы

Service client задаёт auth **reference**, зарегистрированный origin, team scope,
proof profiles и ограниченный scope ceiling. Разрешение — пересечение service
ceiling и **настоящих** user scopes/member teams из того же TeamPolicy.
Body/header admin, email и membership внешнего канала права не выдают.

Actor token передаётся только в `X-Keep-Actor-Token`. Профиль проверяет signature,
exact issuer/audience/authorized party, token-profile claims, `sub`, `iat/exp/nbf`,
freshness и groups claim. `none`/HS algorithms и credential/proof в body запрещены.
Reserved identity/time claims не заменяются `required_claims`.
JWKS cache/timeout — IaC параметры. Для текущей локальной Keycloak-лабы разрешён
HTTP на loopback/`.svc`; публичный HTTP issuer запрещён. Signature/claim/role
checks обязательны при обоих вариантах.
`username_claim: null` использует verified `sub` как Keep username. Для отличающегося
имени/assignee задаётся обязательный signed claim, например `preferred_username`.
Identity и idempotency остаются issuer + sub; токен не сохраняется в receipt.

Subscriber задаёт silence lifecycle types и team-scoped destinations. Создание
SilenceEvent и outbox record — одна DB transaction. Сбой receiver не откатывает
правило и не блокирует ответ пользователю. Один DB/worker delivery стек используется
для service events и ordinary notifications, без нового брокера/второй очереди.

Silence created/updated/activated/cancelled/expired — immutable v1 snapshots;
их нельзя подавлять обычным silence gate или collapse через debounce.
Activation/expiry сохраняют точное effective_at; scan cadence влияет на доставку
события, не на интервал silence. Retries сохраняют event ID/revision/origin;
duplicate/out-of-order receiver восстанавливается по revision и snapshot/cursor.
Receipt, verified command identity и event correlation защищают от echo-команд.

Ordinary notification перед **каждой** отправкой/retry получает актуальные
canonical state, owner/history, route и coverage. Runtime при partial coverage
пропускает весь агрегат с `silence_partial_payload_unsafe`: общий title/manual
text/summary не раскрывает подавленные алерты. Explicit unassigned scope не
разрешает читать инцидент с историей другой команды.
Снятие silence не проигрывает накопленную историю и не запускает remediation снова.

## 8. Примеры и проверки

`examples/a`: cedar/quartz, workload grouping, окно 300s, reopening, ACK 600s,
flap threshold 4, HTTP JSON + Mattermost fan-out, explicit append/link fallback.
`examples/b`: harbor/lumen, grouping реплик по resource, окно 45s, new-incident,
ACK 120s, flap threshold 2, другая service alias/regex и только HTTP JSON.
Оба используют собственные replaceable groups/zones/routes/contacts/таймеры.

Fixtures содержат raw workload/owner/renamed pod/PVC/node/service/missing labels,
разные regex/aliases и ожидаемые changes grouping/reopen/deadline/flapping/delivery.
Contract arithmetic не является тестом нового runtime engine. В k3d дополнительно
проверяются реальный CEL compiler/evaluation, TeamPolicy, MappingRuleDtoIn и pre-parser.

```bash
python3 lab/incident_contract.py validate docs/fork/incident-core/examples/a/bundle.yaml --tenant keep
python3 lab/check-incident-contract.py
python3 lab/check-incident-contract-k3d.py
```

Host checks требуют PyYAML/jsonschema; k3d runner собирает отдельный test image
из локального backend image и pinned `lab/requirements.contract-check.txt`.
`--base-image` выбирает доступную локальную базу; текущий source `keep/` копируется
в test image. Runner импортирует образ в cluster `local`, создаёт isolated
ConfigMap/Job в `k3d-local/keep-lab` и сохраняет inputs/digests/status/logs в
`.lab-work/incident-contract/<UTC>/`. Проверка не подключается к рабочей БД.
Все временные файлы размещаются в явном work directory, без `/tmp`.
