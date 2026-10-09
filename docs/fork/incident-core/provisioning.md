# Применение IncidentPolicies через IaC

## Поддержанный набор

Один bundle на tenant атомарно применяет TeamPolicy v1, mapping, extraction,
legacy rules, workflows, normalization, presentations, correlation, lifecycle,
automation, contacts, routes и конфигурацию integrations/dispatch.
Имена команд, адресатов, grouping rules и transport references задаются документами.
Silence lifecycle использует HTTP JSON; incident notifications поддерживают
HTTP JSON, прямой Mattermost API и отдельный Mattermost transport bridge.

`rules[].artifact` использует текущий `RuleCreateDto`: `ruleName`, `sqlQuery`,
`celQuery`, `timeframeInSeconds`, `timeUnit` и его optional fields.
`extraction[].artifact` использует `ExtractionRuleDtoBase`: `name`, `attribute`,
`regex`, `priority`, `condition`, `pre`, `disabled`, `description`.
Новая группировка задаётся отдельно в `correlation[]`; см. `correlation.md`.

Два самостоятельных примера находятся в `config/incident-policies.example/a`
и `b`. Они отличаются team IDs/groups/zones, видимостью, приоритетами и окном
корреляции. Workflow с provider `mock` показывает формат без внешней отправки;
в рабочем наборе заменяется своим поддержанным provider. Конфигурация proof и
двух HTTP receivers показана в `config/silence-integrations.example`.
Примеры `incident-core/examples/a,b` показывают также доменные секции;
перед активацией задайте свои destinations и credentials references.

## CLI и deployment

Таблицы provisioning вводит additive migration `a83d91ce6f40` после
`9d6a8f3b5e27`; перед запуском используйте текущий Alembic head. Snapshot, история
версий и ownership хранятся в отдельных
таблицах. Downgrade с непустой историей отклоняется, чтобы rollback приложения
не уничтожил конфигурационную историю.

Команды запускаются в окружении backend с его подключением к БД и доверенным
tenant. `validate` не записывает данные, `preview` также не меняет БД:

```bash
python -m keep.api.core.incident_policies_cli validate --bundle config/incident-policies.example/a/bundle.yaml --tenant keep
python -m keep.api.core.incident_policies_cli preview --bundle config/incident-policies.example/a/bundle.yaml --tenant keep
python -m keep.api.core.incident_policies_cli status --tenant keep
```

Apply требует все три значения из проверенного preview. При первом apply
`active_digest: null` передаётся как `none`:

```bash
python -m keep.api.core.incident_policies_cli apply \
  --bundle config/incident-policies.example/a/bundle.yaml --tenant keep \
  --expected-active-digest none \
  --expected-candidate-digest <candidate_digest> \
  --expected-preview-digest <preview_digest>
```

Для declarative deployment задаётся `KEEP_INCIDENT_POLICIES_CONFIG_FILE` — путь
к смонтированному bundle. Startup проверяет весь набор, строит preview и
применяет его в одной транзакции. Ошибка, неподдержанный runtime, исчезнувший
mount или снятая переменная сохраняют последний активный snapshot в БД.
При существующем active bundle старые loaders не заменяют его отдельными
TeamPolicy/mappings/workflows. `PROVISION_RESOURCES=false` отключает startup apply;
сохранённая конфигурация продолжает читаться.

При отсутствии active bundle остаётся совместимый legacy путь: workflows и
mappings проверяются целиком и применяются в общей startup транзакции.
Снятая переменная, пустой/пропавший каталог и исчезнувший файл сохраняют прежние
ресурсы. Одноимённый UI-owned ресурс не принимается по имени. Старый каталог
не является декларацией удаления; явные adoption/deletion выполняются bundle.

## API

`/settings/incident-policies` требует настоящую роль admin. `viewer` и
`responder` не получают глобальную конфигурацию и право apply.

| Метод / путь | Результат |
| --- | --- |
| `GET /settings/incident-policies` | Generation/digest/revision/source, последнее успешное применение, drift и resource digests |
| `POST .../validate` | Проверка всего кандидата |
| `POST .../preview` | Diff, active/candidate/preview digests, apply/noop |
| `POST .../apply` | Атомарное применение с тремя обязательными expected digests |
| `GET .../resources/{kind}/{target_id}` | Digest существующего tenant-scoped ресурса для reviewed adoption |
| `GET .../versions/{generation}/preview` | Preview сохранённой версии |
| `POST .../restore/preview` | Preview сохранённой версии с явным списком удалений |
| `POST .../restore` | Применение проверенной сохранённой версии |

Validate/preview/apply принимают `bundle` и `artifacts`: словарь относительного
пути → точный UTF-8 текст файла. Сервер не открывает произвольные пути из тела.
SHA-256 относится к точным байтам UTF-8, включая переводы строк.
Apply добавляет `expected_active_digest`, `expected_candidate_digest` и
`expected_preview_digest`. Tenant файла сверяется с tenant аутентифицированного
admin. Ошибки содержат расположение/код проверки, без значений credentials.

## Identity, adoption и удаление

Identity — `(tenant, kind, logical_id)` с постоянным внутренним `target_id`.
Изменение имени/path ресурса сохраняет связи. Bundle ID задаёт владельца и
не переименовывается обычным apply. Revision — человеческая метка; generation
и digest определяют опубликованную версию.

UI-owned ресурс с совпавшим именем требует явного adoption:

```yaml
adoptions:
  - kind: mappings
    id: component-ownership
    target_id: "17"
    expected_resource_digest: <digest from reviewed target>
```

Digest существующего target берётся через API выше. Target обязан принадлежать
этому tenant, не иметь другого владельца и не измениться после review.
Повтор adoption с тем же binding даёт no-op. Retired binding сохраняется и не
может быть присвоен другому logical ID.

Omission управляемого ресурса — ошибка. Для удаления ресурс убирается из desired
секции и добавляется reviewed запись с digest из status:

```yaml
deletions:
  - kind: mappings
    id: component-ownership
    expected_resource_digest: <digest from active status>
```

Mapping/extraction выключаются, workflow/rule помечаются удалёнными; строки и
ownership tombstones сохраняются. Нельзя удалить team с alerts/incidents/silences
или rule, на которую ссылаются incidents. Ссылки bundle проверяются до записи.
Такое состояние требует отдельного переноса владения с review.
Повторное применение deletion не меняет generation.

## Версии, drift и восстановление

Preview не содержит artifact values. Повтор неизменного набора без drift не
создаёт snapshot/version, workflow revision, execution или delivery. Apply
повторно проверяет DB state под блокировками. Проигравший конкурентный запрос
получает conflict и не затирает новую версию. Сбой любой записи откатывает
весь набор, включая ownership и active pointer.

HTTP request, ingestion и постановка workflows используют один snapshot на
операцию. Следующая операция перечитывает active version из БД. Auth verifier
перечитывает role/member policy. Старый queued step не вызывает provider после
смены конфигурации; interval workflow также проверяет запланированную revision.
Delivery worker обновляет settings перед попыткой отправки и сохраняет diagnostic
при отзыве подписки. Начатая внешняя операция может завершиться после apply;
применение не обещает отменить уже выполняющийся HTTP/provider вызов.

Settings → Users and Access → Teams показывает активные revision/generation и
digest; admin дополнительно видит source, actor/time/result и число ресурсов
с drift. Управляемые ресурсы защищены обычными CRUD endpoints и UI controls.
Изменение бизнес-полей, provisioning flag/path или исчезновение DB target
попадает в drift; ремонт существующего target требует свежий preview.

Сохранённая версия содержит проверенные artifact bytes и не зависит от прежнего
mount. Восстановление публикует новую generation. Оно не изменяет incidents,
ручные summary/assignee/ACK/resolve, silence audit, command receipts или delivery
history. Если после старой версии появились ресурсы, нужны явные deletions:

```bash
python -m keep.api.core.incident_policies_cli preview --tenant keep \
  --restore-generation 1 --deletions-file reviewed-deletions.yaml
```

`reviewed-deletions.yaml` — YAML список deletion records. Apply использует тот же
источник и свежие три digests. Через API `POST .../restore/preview` получает
`generation` и optional `deletions`; `POST .../restore` получает тот же набор плюс
три expected digests. Проверки ссылок/владения действуют и при restore.
