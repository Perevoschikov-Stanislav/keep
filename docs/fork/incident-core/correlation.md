# Корреляция: runtime и IaC

## Включение

Применить миграции до `e5d04b9ca716`. В validated bundle добавить `correlation`
и связанные `lifecycle`/`presentations`. Публикация — прежние validate → preview
→ apply с тремя ожидаемыми digest. Обычная команда/роль и ownership определяются
TeamPolicy. Имена команд и пути групп берутся из access artifact.

Рабочие примеры: `config/incident-correlation.example/a,b`. Разные наборы команд,
окна 300/60 секунд, пороги 1/2, overlap first_match/parallel, missing fallback
separate_alert/skip_correlation и resolve_on all_resolved/never.
Транспорты уведомлений для корреляции не требуются.

```yaml
correlation_overlap: first_match
correlation:
  - id: workload
    team_ids: [cedar, quartz] # примеры; заменить своими logical IDs
    priority: 100
    match: normalized.kind == 'workload'
    group_by:
      - normalized.cluster
      - normalized.namespace
      - normalized.workload
    required_fields:
      - normalized.cluster
      - normalized.namespace
      - normalized.workload
    missing_required: separate_alert
    window_seconds: 300
    threshold: 2
    create_on: any
    lifecycle_ref: basic
    presentation_ref: workload
lifecycle:
  - id: basic
    resolve_on: all_resolved
    clock: receive_time
    late_event_policy: history_only
    reopen: {mode: new_incident, within_seconds: 0, ack: reset, assignee: clear}
    flapping: {enabled: false, window_seconds: 300, transition_threshold: 4, reset_after_seconds: 600}
```

Все настройки проверяются схемой и runtime compatibility validator.
Event-time/reopen/flapping описаны в `lifecycle.md`; automation_ref — в
`automation.md`; presentation actions и routing — в `notifications.md`.
При ошибке сохраняется активный snapshot. `rules` прежнего формата и новая `correlation` в одном bundle
отвергаются: порядок между двумя движками не подразумевается.

## Состав и ключ

CEL получает canonical DTO сохранённой версии алерта. Приоритет больше — правило
раньше. При равном приоритете внутри команды конфигурация отклоняется.
CEL errors и не boolean результат дают diagnostics, исходный алерт остаётся.

`first_match` выбирает первое подходящее правило. Нехватка обязательного поля
в нём **не** передаёт событие более широкому правилу. `parallel` применяет все
подходящие правила, каждому принадлежит свой состав и ключ.

Ключ — SHA-256 канонического JSON tuple: версия формата, tenant, canonical team,
logical rule ID, rule revision и упорядоченные пары path/typed value. Revision
включает правило, связанную lifecycle policy и overlap. Строки, числа, boolean,
null, missing, списки и словари имеют разные type tags; разделители и порядок
ключей словаря не меняют границы значений. Две команды всегда имеют разные ключи.
Изменение одной презентации/маппинга не меняет revision правила само по себе.

Каждый group_by обязателен в required_fields. Missing/null/empty и неизвестные
normalized значения не дают общий ключ. Diagnostics содержат path и состояние.
`skip_correlation` сохраняет алерт без включения; `separate_alert` создаёт ключ
по его canonical fingerprint, без смешивания нескольких неполных объектов.
Нормализация и original AlertRaw описаны в `normalization.md`.

Реплики объединяются через workload, PVC/node/service — через отдельные правила
и нужные им поля. Совпадение только service/времени не добавляет необъявленную связь.

Для multi-level: `multi_level: true`, один dictionary path в group_by и
`multi_level_property_name` — путь внутри каждой записи. Одинаковые значения
не создают второй инцидент. Некорректная запись применяет объявленный missing
fallback ко всему алерту; исправные части не объединяются скрыто при ошибке.

## Окно, порог и повторы

Окно фиксированное `[first received, first received + window_seconds)` по
сохранённому DB timestamp; оно не продлевается повторами. На верхней границе
возникает следующий эпизод со ссылкой на предыдущий. Закрытый/удалённый/merged
инцидент не принимает новую firing версию. Ссылка на эпизод другой команды
не создаётся даже при административной смене владельца.

До порога в БД существует невидимый кандидат для сохранения состава, в UI и
workflow incident.created он ещё не публикуется. Порог считает **разные текущие
firing fingerprint**, а не версии/доставки/счётчик unresolvedCounter.
`create_on: all` дополнительно требует покрытия каждой верхней OR ветки match
связанными firing алертами. Вложенные AND/OR внутри ветки остаются одним условием;
conditional CEL expressions для all отклоняются. `any` проверяет полный CEL.

Одна сохранённая Alert.id обрабатывается один раз. Повторные версии одного
fingerprint обновляют canonical состав без увеличения порога. Неактуальная
LastAlert версия получает history_only; она не переносит группу назад.
Удалённый вручную link не возвращается повтором. Ручные title, notes, assignee,
ACK и forced severity не заменяются автоматически.

## Транзакция и диагностика

Новая таблица `incidentcorrelationgroup` содержит уникальную строку ключа и
текущий incident ID. В одной транзакции сохраняются group/episode, membership,
порог/состояние, aggregate presentation и evidence конкретной Alert.id.
Ошибка до commit откатывает всё; исходный алерт уже сохранён отдельно.

Порядок блокировок: сохранённая версия алерта → tenant allocator → отсортированные
group keys → инцидент. PostgreSQL tenant lock использует FOR NO KEY UPDATE,
совместимый с FK key share. Это устраняет обнаруженный deadlock FK/allocator.
Tenant lock также сериализует назначение native running_number; создание
manual и correlated инцидентов использует один allocator. Следствие: корреляция
внутри tenant сериализована до commit; пропускная способность зависит от
времени удержания общей блокировки.

В `Incident.correlation_context` закреплены rule ID/revision, configuration digest,
match, overlap, typed group values, window bounds, threshold, presentation_ref
и lifecycle. В `Alert.correlation_context` — решение по каждому правилу,
missing fields, fallback и incident ID. Клиентские/enrichment claims этих полей
не могут заменить серверное объяснение. В dedup hash объяснения не входят.

Incident overview показывает правило/версию, состав, ключевые признаки,
окно/порог/resolve policy; подписи берутся из presentation.fields IaC.
Alert sidebar показывает причины включения/отказа. Всё работает без моста.

Поддержаны resolve_on all_resolved/first_resolved/last_resolved/never
по полному canonical составу. Lifecycle, ACK/reopen/flapping и arbitration
с поздними переходами описаны в `lifecycle.md`. Политику созданного IaC-инцидента нельзя
перезаписать обычным edit: API возвращает 409, UI отключает её изменение.

## Изменение конфигурации и прежние правила

Preview содержит `correlation_impact`: changed rule IDs, количество затронутых
открытых инцидентов, scope new_groups и rewrite_history=false. Для first_match
учитываются пересекающиеся команды других правил. Изменение group_by/match/
priority/window/lifecycle/overlap даёт новую revision для следующего принятого
события. Старые контексты и membership остаются. Перепривязка требует отдельного проверенного migration plan.
Как и раньше в Keep, membership ссылается на fingerprint/LastAlert; новая версия
того же fingerprint доступна старому link, сохранённая история Alert не переписывается.

Прежние UI/`rules` остаются доступными отдельно. Их `_calc_rule_fingerprint`
также использует typed scoped key, не падает на отсутствующем промежуточном
объекте и не создаёт общий none. Создание инцидента защищено durable group lock.
Старые comma/none ключи не переиспользуются новым кодом: следующая корреляция
начинает группу формата legacy-v2, старая история сохраняется. Перед обновлением
сборки этот переход требуется учесть в migration plan. Полный pinned explanation
и новый overlap доступны в `correlation`, не в legacy Rule DTO.

Workflow dispatch после commit использует существующий Keep механизм.
Automation jobs и ordinary notification outbox описаны в `automation.md`
и `notifications.md`.
Миграция не переносит инциденты; downgrade отказывается удалять накопленное
correlation history или строки group.
