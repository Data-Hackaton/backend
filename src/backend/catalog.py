"""Каталог типов событий для интерфейса: стандартные — из reference/scenarios.yaml (правила L3 и шаги процедуры),
собственные — из БД (EventType). id устойчивые: тот же type_id отдаётся у событий (GET /events)."""

import hashlib

import yaml

from backend import config

SOP_TYPES = {  # предупреждения SOP-чекера (по схеме NVIDIA) — event = sop_step
    "Пропущен шаг процедуры": "sop:missing",
    "Шаг процедуры не по порядку": "sop:misordered",
}


def rule_type_id(title: str) -> str:
    return "rule:" + hashlib.sha1(title.encode()).hexdigest()[:10]


def why_text(why: str | None) -> str:
    """Пояснение правила без ссылок на видео эталона «(X-1)»."""
    text = (why or "").split(" (X-")[0].strip()
    return text[:1].upper() + text[1:] if text else ""


def severity(inv: dict) -> str:
    return inv.get("severity") or ("violation" if inv.get("norm") else "warning")


_SPEC: dict | None = None


def spec() -> dict:
    global _SPEC
    if _SPEC is None:
        path = config.META_DIR / "reference" / "scenarios.yaml"
        _SPEC = yaml.safe_load(path.read_text()) if path.exists() else {"scenarios": {}, "vocabulary": {"actions": {}}}
    return _SPEC


def standard_types() -> list[dict]:
    """Правила L3 (по одному на заголовок) + предупреждения SOP-чекера + шаги процедуры."""
    s = spec()
    rules = [inv for sc in s.get("scenarios", {}).values() for inv in sc.get("invariants", [])]
    rules += s.get("video_invariants", [])
    vocab = s.get("vocabulary", {}).get("actions", {})
    out: dict[str, dict] = {}
    for inv in rules:
        title = inv.get("title")
        if not title:
            continue
        tid = rule_type_id(title)
        if tid in out:  # одно правило в нескольких сценариях: самый строгий статус
            if severity(inv) == "violation":
                out[tid]["kind"] = "violation"
            continue
        action = inv.get("action") or inv.get("then") or {}
        name = action if isinstance(action, str) else action.get("action") if isinstance(action, dict) else None
        out[tid] = {"id": tid, "title": title, "description": why_text(inv.get("why")),
                    "observable": vocab.get(name, "") if name else "", "kind": severity(inv), "custom": False,
                    "status": "published", "samples": [], "norm": inv.get("norm")}
    for title, tid in SOP_TYPES.items():
        out[tid] = {"id": tid, "title": title, "description": "SOP-чекер: обязательный шаг цикла обслуживания "
                    "пропущен или идёт не по порядку", "observable": "", "kind": "warning", "custom": False,
                    "status": "published", "samples": [], "norm": None}
    for action, text in vocab.items():
        tid = f"step:{action}"
        out[tid] = {"id": tid, "title": text[:1].upper() + text[1:], "description": "Шаг процедуры",
                    "observable": text, "kind": "expected", "custom": False, "status": "published", "samples": [],
                    "norm": None}
    return list(out.values())


def event_type_id(event: str, status: str, title: str | None, rules: list | None, source: str) -> str:
    """type_id события модели или человека (у ручного события тип хранится в rules[0])."""
    if source == "human" and rules:
        return rules[0]
    if title in SOP_TYPES:
        return SOP_TYPES[title]
    if status != "expected" and title:
        return rule_type_id(title)
    return f"step:{event}"
