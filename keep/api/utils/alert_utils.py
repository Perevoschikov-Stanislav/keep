def sanitize_alert(alert_raw: dict) -> dict:
    """
        Recursively sanitize alert data by removing null characters.
        The function could be used to remove/replace any unwanted characters
        from the alert data structure, ensuring that the data is clean and safe
        for further processing or storage.

        Args:
            alert_raw (dict): The raw alert data
    """
    if alert_raw is None:
        return None

    if not isinstance(alert_raw, dict):
        raise ValueError("Input must be a dictionary")

    def sanitize(value):
        if isinstance(value, str):
            return value.replace('\x00', '')
        elif isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [sanitize(i) for i in value]
        return value

    return sanitize(alert_raw)


DEFAULT_SERVICE_LABELS = [
    "deployment",
    "statefulset",
    "daemonset",
    "app",
    "app.kubernetes.io/name",
    "app_kubernetes_io_name",
    "upstream",
    "persistentvolumeclaim",
    "pvc",
    "job_name",
    "container",
    "pod",
    "service",
]

DEFAULT_EXCLUDE_SERVICES = {
    "prometheus-kube-state-metrics",
    "kube-state-metrics",
    "prometheus-kube-prometheus-kubelet",
    "kubelet",
    "prometheus-prometheus-node-exporter",
    "prometheus-node-exporter",
    "node-exporter",
    "prometheus-operator",
    "alertmanager",
}


def get_service_candidate_labels() -> list[str]:
    from keep.api.core.config import config
    raw = config("KEEP_SERVICE_LABELS", default="")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return list(DEFAULT_SERVICE_LABELS)


def get_exclude_services() -> set[str]:
    from keep.api.core.config import config
    raw = config("KEEP_EXCLUDE_SERVICES", default="")
    if raw:
        custom = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return custom | DEFAULT_EXCLUDE_SERVICES
    return set(DEFAULT_EXCLUDE_SERVICES)


def is_service_excluded(val: str, exclude_set: set[str], candidate: str) -> bool:
    val_lower = val.lower()
    if val_lower in exclude_set:
        return True
    if any(val_lower.startswith(ex) for ex in exclude_set):
        return True
    if "-exporter" in val_lower or "_exporter" in val_lower:
        return True
    return False


def extract_service_from_alert(alert_data: any) -> str | None:
    """
    Extracts meaningful service/workload from an alert event, AlertDto, or dict.
    Filters out exporter infrastructure services (e.g. prometheus-kube-state-metrics).
    Configurable via KEEP_SERVICE_LABELS and KEEP_EXCLUDE_SERVICES.
    """
    if alert_data is None:
        return None

    if hasattr(alert_data, "dict") and callable(alert_data.dict):
        try:
            alert_data = alert_data.dict()
        except Exception:
            pass

    if isinstance(alert_data, dict):
        labels = alert_data.get("labels") or {}
        annotations = alert_data.get("annotations") or {}
        get_val = alert_data.get
    else:
        labels = getattr(alert_data, "labels", None) or {}
        annotations = getattr(alert_data, "annotations", None) or {}
        get_val = lambda k, default=None: getattr(alert_data, k, default)

    exclude_services = get_exclude_services()
    candidate_labels = get_service_candidate_labels()

    for candidate in candidate_labels:
        val = get_val(candidate)
        if not val and isinstance(labels, dict):
            val = labels.get(candidate)
        if not val and isinstance(annotations, dict):
            val = annotations.get(candidate)

        if val and isinstance(val, str):
            val = val.strip()
            if not val:
                continue
            if is_service_excluded(val, exclude_services, candidate):
                continue
            return val

    return None
