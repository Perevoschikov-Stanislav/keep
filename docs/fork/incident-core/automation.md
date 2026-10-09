# SLA, эскалации, напоминания и workflows

## IaC и привязка к инциденту

`correlation[].automation_ref` выбирает `automation[]`. Условия создания остаются
в correlation match/create_on/threshold; дополнительные условия действия задаются
существующим workflow YAML. Невидимые кандидаты не выполняют действия.
`automation[].match` (CEL, default `true`) ограничивает всю policy
по `normalized`, `incident.status/severity` и `team_id`. False отменяет ожидающие
операции; возврат к true сохраняет origin/cursors вместо нового SLA отсчёта.
Начатые внешние эффекты не отзываются.
`contacts[]` и automation становятся managed resources с обычным preview/apply,
ownership, удалением и восстановлением конфигурации. Routes и notification DTO
описаны в `notifications.md`.

Настройки: `sla_start`, `ack_deadline_seconds`, `stop_on`, `on_reopen`,
`on_policy_update`, levels/offsets/повторы, reminder cadence, contact/destination/workflow
references, ticket workflow/URL/enrichment key и `execution_lease_seconds`.
Cadence и размер обработки worker берутся из существующего `dispatch`.
Идентификаторы команд, адресаты, transport references и интервалы являются данными.
Два заменяемых примера: `config/incident-automation.example/a,b`.
Они используют разные команды/интервалы и два HTTP transport reference в одной цепочке.
`record-example` — mock workflow для проверки конфигурации, реальные provider credentials
и создание тикета в сторонней системе в этих примерах не настроены.

## Время и жизненный цикл

SLA origin — время первого принятого эпизода из lifecycle clock. В режиме
`created` оно сохраняется при same-ID reset; `last_reopen` начинает отсчёт заново
при reset. История start_time из linked alerts не сдвигает этот origin.
В режиме `continue` сохраняются origin/cursors. Новый ID всегда начинает новую цепочку.
Начальная привязка происходит, когда кандидат становится видимым; origin остаётся
временем первого принятого события, даже если threshold достигнут позже.

| Событие | Эффект |
| --- | --- |
| ACK не позже deadline | Breach отсутствует; если ACK включён в stop_on, ожидающие операции отменяются |
| Поздний ACK | Breach сохраняется; остановка определяется stop_on |
| Resolve/merge/delete | Остановка и отмена ожидающих операций; уже начатое внешнее действие нельзя отозвать |
| Same-ID reopen/reset | Отмена старых jobs; новый chain/episode, активная automation policy и новые deadlines |
| Same-ID reopen/continue | Сохранение origin/cursors, без повторения обработанных операций; активная policy следующего эпизода применяется с сохранённым origin |
| New incident ID | Новый origin/chain, ACK и SLA не наследуются; ticket link старого ID не копируется автоматически |

Точные границы: deadline/level/repeat становятся due при `now >= at`;
ACK ровно в deadline не считается breach. Offset каждого уровня считается от origin.
`repeat_limit` включает первый запуск; `0` означает повтор до следующего уровня/stop,
`1` — один запуск. `repeat_every_seconds: 0` выключает повтор.
После достижения следующего уровня предыдущий больше не повторяется.

## Изменение конфигурации

По умолчанию `on_policy_update: next_episode`: текущий эпизод сохраняет policy,
deadlines и выбранные references. Следующий эпизод использует active policy,
включая новый выбор `continue/reset`.

- `reschedule`: явно заменяет текущую цепочку из прежнего SLA origin, отменяет
  ожидающие операции старой policy; текущий уровень выбирается без исторического replay.
- `cancel`: останавливает текущую цепочку и отменяет ожидающие операции.
- Preview показывает режим, число затронутых открытых инцидентов и сохранение origin.
- Повторный apply с тем же digest не создаёт вторую цепочку. Restore ранее
  обработанной policy не оживляет завершённые business keys.
- Удалённая policy сохраняется у текущей цепочки; следующий эпизод не получает
  удалённую automation, в том числе в режиме continue.
- Workflow binding включает logical ID, внутренний ID и revision. Изменение,
  выключение/удаление workflow отменяет старую операцию; новый workflow не подставляется
  молча в ранее запланированный job. Contact retirement/team reassignment также проверяется.

## Постоянное состояние и исполнение

`Incident.automation_context` содержит policy/version, origin/deadline, episode,
chain, breach, уровень, cursors, bindings и последние решения/результаты.
Публичный `IncidentDto.automation` скрывает bindings, addresses и внутреннюю policy.
Обычные incident CRUD/enrichment не могут подменить эти поля.

`IncidentAutomationOperation` хранит business key, tenant/team/canonical incident,
episode/policy/level/ordinal/target, due time, результат, lease/token и WorkflowExecution ID.
Business key вычисляется из incident/chain/policy/level/повтора/назначения.
Для ticket creation ключ сохраняется на весь canonical incident, включая same-ID reopen.
Эта таблица описывает бизнес-операции; delivery retries/bindings относятся к существующему
outbox уведомлений, отдельная транспортная очередь не вводится.

Материализация/cursors/audit коммитятся одной транзакцией под общей tenant/incident
сериализацией. Два worker не создают два решения или два исполнения одного key.
Claim атомарно сохраняет token и WorkflowExecution/incident link. Scheduler использует
существующий executor WorkflowScheduler и его ограничение workers.

Workflow strategies сохраняются: `parallel` допускает независимые операции,
`nonparallel` пропускает пересечение, `nonparallel_with_retry` откладывает операцию.
При неопределённом результате предыдущего запуска nonparallel цепочка ждёт recovery.
Отложенный busy job не занимает поток; его следующая проверка определяется IaC cadence.
Неизвестная strategy отклоняется при проверке IaC до apply.

Перед шагом/попыткой provider заново проверяются owner, canonical status/revision,
episode/chain/policy, workflow revision, contacts и token. ACK/resolve/reassignment
после enqueue/claim останавливают следующее действие. Изменение revision во время
выполнения отменяет остаток workflow. Событие, уже принятое внешним API, не отменяется.
Thread-local workflow context восстанавливается после выполнения, логи следующего
задания не получают чужой execution ID.

Истёкший lease без начатой попытки можно reclaim с тем же WorkflowExecution ID
и новым token. После начатой попытки результат `uncertain`, автоматического повторения
этой операции нет. Внешний recovery/receipt/idempotency описан в `notifications.md`.
Настроенные provider retries продолжают использовать существующий workflow engine;
перед каждой попыткой проходят те же status/config/silence gates. Exactly-once
для стороннего API не заявляется.

## Пауза и сайленсы

При простое выбирается последний due repeat/reminder. Старые уровни выполняют
первое обычное действие workflow, но notification actions получают
`superseded_level`/`superseded_repeat`. Уже поставленные jobs и выполняющийся workflow
проверяют актуальный уровень перед отправкой, даже без промежуточного scan.

Сайленс не останавливает breach/уровни/cursors и обычные действия.
Каждый action, включая on-failure, у автоматизированного workflow обязан явно
объявлять `notification: true/false`; непомеченная активация отклоняется.
Notification actions используют silence gate, включая fail-closed partial coverage.
Silenced job сохраняет skip, а не подтверждённую доставку. Снятие сайленса не
перезапускает пропущенный repeat или remediation; следующий due использует текущее состояние.

Destination-only/reminder создаёт scoped operation `awaiting_dispatch`, либо skip
с причиной. Contact references фильтруются по canonical team; чужого fallback нет.
Operation передаётся в общую очередь уведомлений. На фактической отправке
повторно проверяются актуальные coverage/state. Notification workflows также
проходят silence gate.

## Тикеты, UI и миграция

Ticket workflow запускается один раз для canonical incident. Provider/configuration
задаются стандартным workflow YAML. `url_template` строится из результатов workflow
и canonical incident; значения для path/query URL-encoded. Credentials и reserved
incident fields в template/enrichment key отклоняются. Пустой результат поля не
порождает фиктивную успешную ссылку.
Существующая ручная ссылка пропускает creation. Ссылка, добавленная во время provider,
сохраняется и при workflow enrichment, и при записи итогового URL. Изменение команды
проверяется общим locked domain service. Ticket update остаётся обычным workflow;
провайдерские неопределённые результаты требуют recovery, повтор не создаётся автоматически.
Сессия provider enrichment закрывается в finally: успешное обогащение и ошибка
не удерживают соединение/транзакцию PostgreSQL после завершения действия.

Incident overview показывает ACK deadline/breach, уровень/episode, policy revision,
следующую оценку, stop/последнее решение и результат/skip каждого действия.
После остановки цепочки следующая оценка очищается.
Execution ID связывает операцию со стандартными workflow runs/results/logs;
level transitions и результаты исполнений пишутся в audit. Отмены ожидающих jobs
связаны с canonical lifecycle audit или историей preview/apply конфигурации.

Аддитивная миграция `6b2fda31c890` после `f38a21d67b04` добавляет JSON state/table/indexes.
История/ручные поля не переписываются; downgrade отказывается удалять заполненное состояние.
Перед запуском новой сборки применяются миграции до текущего Alembic head.
Проверочная k3d PostgreSQL изолирована от основной lab DB. Проверки restart/claims
используют новые workers/sessions и постоянную БД.
