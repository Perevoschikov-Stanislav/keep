# жизненный цикл инцидентов

## Настройка

`lifecycle` задаётся в IncidentPolicies bundle и подключается через
`correlation[].lifecycle_ref`. Schema и каталог параметров — общие с IncidentPolicies.
Validate/preview/apply/restore описаны в `provisioning.md`.
Новая nullable миграция `f38a21d67b04` идёт после `e5d04b9ca716`.
Она добавляет `Incident.lifecycle_context` и `IncidentCorrelationGroup.lifecycle_state`.
Исторические записи и ручные поля не переписываются. Downgrade при накопленном
состоянии запрещён до отдельного экспорта/проверки.

Два исполняемых примера: `config/incident-lifecycle.example/a,b`.

| Параметр | a | b |
| --- | --- | --- |
| Команды | platform-blue, database-green | frontend-west, network-east |
| clock | receive_time | event_time |
| reopen.mode | reopen | new_incident |
| reopen.within_seconds | 300 | 0 |
| ACK | preserve | reset |
| assignee | preserve | clear |
| flap window / threshold / quiet reset | 300 / 4 / 600 | 120 / 2 / 60 |

Имена команд и role groups находятся в bundle/artifacts; в runtime нет списка
OPS/IT, каналов или зависимости от транспорта.

## Время и текущие версии

- `receive_time` использует сохранённый `Alert.timestamp` — время приёма Keep,
  а не время запуска rules worker. `event_time` использует исходный
  `Alert.event.lastReceived`, приведённый к UTC. `startedAt` не используется.
  Входной AlertDto уже проверяет/нормализует `lastReceived`.
- Нет скрытого fallback с ошибочного provider time на receive time: такая
  запись получает `invalid_event_time`, сохраняется, но не меняет группу.
- В группе сохраняются watermark и принятые версии каждого fingerprint.
  Событие раньше watermark — `history_only`. При одинаковом времени
  конфликтующие состояния одного fingerprint также остаются историческими.
  Разные fingerprints с одинаковым временем могут быть приняты.
- Глобальная проверка принятых source cursors не позволяет старому событию
  создать другую группу, просто изменив workload/group_by. Текущий canonical
  owner проверяется отдельно: предыдущая версия другой команды не участвует.
- `LastAlert` остаётся индексом текущего приёма. Lifecycle использует сохранённые
  принятые версии: поздний raw firing не отравляет последующее all-resolved.
  Те же версии используются для агрегатов/title/source/severity инцидента.
- `Alert.correlation_context` фиксирует решение один раз на сохранённую версию.
  Повторная доставка этой версии не запускает переход ещё раз.

Политика history_only относится к состоянию инцидента. Очередь уведомлений
использует принятые версии canonical state.

## Эпизоды и ручные поля

При firing после resolved:

1. `reopen` и `resolved_at <= time < resolved_at + within_seconds` возвращают
   тот же ID. На верхней границе, при нулевом окне, `new_incident`, merged/deleted
   создаётся новый ID с `same_incident_in_the_past_id`.
2. Повторное открытие очищает текущий end_time/resolve timestamp, увеличивает
   episode, задаёт episode_start/reopened_at. Это новый якорь для SLA;
   исторические start_time, связи, названия, заметки и аудит сохраняются.
3. ACK preserve возвращает acknowledged, если предыдущий эпизод был взят;
   reset возвращает firing. Новый ID всегда firing, без старого ACK.
4. Assignee сохраняется или очищается строго по reopen policy. Resolve сам
   исполнителя не меняет. Назначение меняет explicit Assign/assignee или новый
   ручной ACK. Повтор того же статуса не забирает инцидент у исполнителя.
5. Исторические resolved-связи сохраняются, но не могут немедленно закрыть
   reopened episode под first/last policy. Для этих двух политик учитывается
   порядок членов текущего эпизода, включая продолжающиеся активные алерты.
   All-resolved и full/partial показатели используют весь текущий canonical состав.

Ручное переоткрытие того же ID разрешено внутри той же reopen policy. Если
политика требует новый ID, API отвечает `409`: новый эпизод создаёт firing event.
Уже заменённый старый ID нельзя вновь активировать параллельно текущему эпизоду.

First/last используют порядок первого принятого события члена в выбранных
часах. Проверки full/partial и autoresolve не ограничены страницей 500.
`never` отключает только автоматическое закрытие; явный resolve остаётся доступен.

## Сервис переходов, права и атомарность

`incident_lifecycle.lock_incident/transition` используются для status API,
автозакрытия, explicit alert enrichment и incident/workflow enrichment.
Обычный PUT не принимает status: нужен status endpoint. Legacy/native autoresolve
тоже пишет статус и audit через этот сервис. Alert fingerprint и incident ID имеют явный тип цели;
совпадение UUID не переключает обновление алерта на другой инцидент.
API повторно проверяет роль, canonical team и историю связанных владельцев
после получения блокировок. Workflow incident enrichment проверяет текущую
команду против incident context и не позволяет delete/merge через enrichment.
Роль/команда интеграционной команды должны поступать из проверенной identity;
транспортных callback handlers здесь не добавляется.

Порядок блокировок: tenant `FOR NO KEY UPDATE` → group → incident.
Correlation дополнительно сериализует saved alert version. Tenant lock также
согласует allocation running_number и чтение source cursors. Это сохраняет
сериализацию корреляции: throughput в одном tenant сериализуется.

Status request поддерживает `expected_revision >= 0`. UI передаёт текущую
revision; несовпадение возвращает `409`, обновляет данные и не меняет локально
статус. Клиенты без revision сериализуются по canonical состоянию в БД.
Никакой retry не подставляет новую revision за пользователя.

## Флаппинг

- Group ID стабилен между эпизодами одной rule/lifecycle revision; новый
  Incident ID не сбрасывает историю колебаний этой группы.
- Считаются смены firing ↔ resolved. ACK относится к активной фазе; первоначальный
  firing, duplicate/repeated resolved, merge/delete не добавляют колебание.
- Окно скользящее: `time - window < transition_time <= time`.
  При достижении threshold классификация включается, фиксируется since.
- Классификация сохраняется до quiet reset, даже если старые переходы выходят
  из окна. На точной границе reset_after_seconds очищаются классификация и
  история текущего окна; следующая смена начинает новое окно с одного перехода.
- Без событий DTO вычисляет истечение quiet reset по UTC времени чтения,
  ничего не записывая. Сохранённое окно очищается при следующем принятом событии.
  Отправка сообщения о reset и worker timers описаны в `notifications.md`
  и `automation.md`.
- При disabled flapping классификации нет, статусный аудит всё равно хранится.

UI показывает episode/start, clock/policy version, full/partial resolve,
flapping/count/window/threshold/since/last transition и время переоткрытия.
Состояние хранится в PostgreSQL; новый rules worker/Session читает его из БД.
Недоступность доставки не отменяет уже зафиксированные переходы.

При изменении lifecycle через IaC preview показывает affected open incidents.
Существующие группы сохраняют pinned policy/счётчики и историю. Новый hash
rule+lifecycle+overlap создаёт новую group revision; старые члены ещё могут
закрыть старый инцидент по старой политике. Автоматического переноса ACK,
счётчиков и ID между revisions нет; миграция политики требует отдельного проверенного плана.

## Сайленсы и транспорт

Сайленс подавляет доставку, а не state/audit/flapping. При reopening того же ID
явный ID selector остаётся применим. Новый ID не получает selector прошлого
эпизода через общий fingerprint: более поздние события исключаются из scope
закрытого IaC эпизода. Старый ID и его resolved notification сохраняют покрытие.
Fingerprint/filter selectors сохраняют своё обычное действие.

Для двух одновременно активных инцидентов по одному алерту прямое правило первого
может покрывать общий алерт второго, без рекурсии. Доставка выполняется через
notification gates. Durable outbox, routing и SLA описаны в `notifications.md`
и `automation.md`.
