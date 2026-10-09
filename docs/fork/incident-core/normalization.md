# Нормализация и представление событий

## IaC

В bundle задаются `normalization[]` и `presentations[]`. Каждое правило
задаёт `team_ids`, CEL `match`, `priority`, упорядоченные `fields[].sources`,
необязательный `extract` и обязательную политику `missing`. Более высокий priority
проверяется первым; применяется первое совпавшее правило в canonical team события.
Ошибку вычисления CEL нельзя принять за успешное совпадение.

`presentation_ref` в normalization выбирает шаблон для Keep до появления routing.
Примеры с двумя разными наборами команд: `config/event-normalization.example/a,b`.
Имена команд, zones, aliases, pod regex и шаблоны являются данными конфигурации.
Оба примера принимаются runtime валидатором. Перед применением замените
данные на конфигурацию своей инфраструктуры.

Sources поддерживают `labels.owner`, `labels.workload` и quoted dictionary keys,
например `labels["app.kubernetes.io/name"]`. Выбирается первый непустой источник.
Пробельные строки считаются пустыми, нестроковое значение отмечается invalid_type.
Regex применяется к выбранному значению; извлечение имени pod объявляется отдельным
правилом с меньшим priority. Исходные labels остаются в событии.

Доступные outputs: cluster, environment, namespace, kind, resource, workload,
service. Произвольные security-поля не являются outputs. `missing: literal`
даёт отображаемый fallback с `known: false`, а `mark_unknown` — null. Оба сохраняют
диагностику; literal нельзя использовать как подтверждённую идентичность объекта.

## Порядок ingestion

1. Копия входного события; существующая pre-extraction и provider formatting.
2. Проекция post-extraction и mappings без DB/audit writes под предварительным
   fingerprint. Из её zone определяется canonical team по TeamPolicy.
3. Нормализация в закреплённом snapshot конфигурации.
4. Окончательный custom fingerprint, явный fingerprint override и hash dedup.
5. Сохранение source snapshot, обычные enrichments, закрепление canonical ownership,
   workflows и существующая корреляция.

Входные `normalized`, `normalization`, `presentation` заменяются результатом Keep.
Fingerprint enrichments не подменяют эти поля при ingestion и чтении API.
Если фактический mapping изменил owner относительно предварительной проекции,
чужая проекция удаляется. Team scope не выводится из нормализованного service.

При нормализации оригинальный вход сохраняется в существующем AlertRaw даже с
`KEEP_STORE_RAW_ALERTS=false`. Это увеличивает объём исходных событий в БД; для
большого потока нужен действующий retention/план хранения. Нормальное событие и его
статус сохраняются независимо от полноты нормализации. API error alerts уже требует
глобальный доступ; нормальные raw snapshots через него не выдаются.

## Fingerprint и неполные поля

Legacy fingerprints не меняются. Custom fingerprint, содержащий `normalized.*`,
кодирует имена и значения полей, а также canonical team в однозначном JSON payload
`normalized-v1`. Это исключает совпадение границ строк и команд. Tenant остаётся
частью ключа хранения Keep. Если normalized identity неполна, сохраняется исходный
provider fingerprint; неизвестный объект не получает hash пустого набора значений.

Hash dedup учитывает normalized values, исключает provenance/config digest и
presentation. Смена шаблона или версии с теми же значениями не создаёт переход
состояния. Ignore для отсутствующего вложенного поля безопасен. Обычная корреляция
пропускает событие, если её `groupingCriteria` содержит неизвестное `normalized.*`;
алерт остаётся сохранённым. Полная политика корреляции и typed group keys описаны в `correlation.md`.

## API, UI и ручной текст

Alert API/workflow context содержат `normalized`, `normalization` с policy/digest,
источником, способом и причиной отсутствия каждого поля, и готовое `presentation`.
Представление содержит title, description, ordered fields, links, missing_fields и
необязательный severity_color. Эти данные использует builder NotificationDTO;
доставка описана в `notifications.md`.

Incident хранит отдельные `generated_name` и `normalization_context`. Состав проекции
пересчитывается при linking, unlinking и обновлении связанного алерта. Берутся только
текущие linked snapshots того же tenant/team. Несогласованные значения становятся
unknown с multiple_values. Источники и версии нормализации остаются в metadata.
Normalized service используется для агрегирования affected services.

`user_generated_name`, `user_summary` и assignee остаются ручными данными. Корреляция
обновляет generated name, а не ручное имя. Ручное имя имеет приоритет. Частичное
обновление metadata не очищает notes. Смена owner очищает чужое представление.
Операторский create/update не принимает новые server-owned поля.

UI показывает generated title в списках/связях, объект, поля, ссылки и incomplete
data в sidebar/overview. Generated text выводится как текст, а переменные URL
экранируются; ссылки с неизвестными параметрами не создаются. Ручной HTML summary
сохраняет существующий renderer. Форма редактирования использует только manual
values и показывает generated title как placeholder. Responder не отправляет
неразрешённые policy fields из формы.

Шаблоны читают только разрешённые пути, включая `normalized.*`, `keep_url` и
`incident.id/status/severity/team_id`. Действия с подтверждением в Keep, доставка
и возможности адаптеров описаны в `notifications.md`.

## Обновления и миграция

Новые aliases/regex/priority влияют на новые события. Apply/restore не переписывает
сохранённые source snapshots, fingerprints, историю, manual notes или ACK. Preview
содержит normalization_impact: new_events, rewrite_history=false и требование
reviewed identity transition. Переход по identities требует отдельного проверенного migration plan.

Presentation читается из active конфигурации при следующей API проекции. Сохранённый
generated_name обновляется при следующем изменении состава/события; SQL поиск name
использует этот сохранённый cache. После изменения только template новые строки
отображения и SQL name filter могут различаться до refresh.

Миграция `c27f8d21b409` после `a83d91ce6f40` добавляет два nullable столбца.
Исторические ручные/автоматические имена в user_generated_name не разделяются
задним числом. Downgrade с заполненными derived columns требует экспорт/review.
