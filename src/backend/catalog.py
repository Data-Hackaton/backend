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


def load_norms() -> dict:
    """Нормы: корпус (norms_corpus.yaml, полные тексты) → аудит пакета проверки (norms_review.yaml, требование и
    границы применимости) → выверенные вручную norms.yaml; поля следующего файла поверх полей предыдущего."""
    out: dict[str, dict] = {}
    for name in ("norms_corpus.yaml", "norms_review.yaml", "norms.yaml"):
        path = config.META_DIR / "reference" / name
        for key, norm in ((yaml.safe_load(path.read_text()) or {}) if path.exists() else {}).items():
            out[key] = {**out.get(key, {}), **norm}
    return out


def load_topics() -> dict:
    path = config.META_DIR / "reference" / "topics.yaml"
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def procedures(inv: dict, topics: dict) -> list[dict]:
    """Процедуры проверки темы (нормативный корпус): что проверить человеку, шаги с наблюдаемостью по видео."""
    out = []
    for tid in inv.get("topics", []):
        t = topics.get(tid)
        if t:
            out.append({"id": tid, "title": t["title"], "check": t.get("check", []), "limits": t.get("limits"),
                        "steps": [{"action": s["action"], "observability": s.get("observability")}
                                  for s in t.get("steps", [])]})
    return out


def rule_description(inv: dict, norms: dict) -> str:
    """Пояснение правила, требование прямого норматива и косвенные / условные основания — для каталога типов."""
    parts = [why_text(inv.get("why"))]
    norm = norms.get(inv.get("norm") or "")
    if norm and norm.get("requirement"):
        parts.append(f"Требование ({norm.get('short', inv['norm'])}): {norm['requirement']}")
    by_relation: dict[str, list[str]] = {}
    for b in inv.get("basis", []):
        if b["norm"] in norms:
            by_relation.setdefault(b["relation"], []).append(norms[b["norm"]].get("short", b["norm"]))
    parts += [f"Основание ({rel}): {'; '.join(names)}" for rel, names in by_relation.items()]
    return ". ".join(p.rstrip(".") for p in parts if p) + ("." if any(parts) else "")


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
    norms, topics = load_norms(), load_topics()
    out: dict[str, dict] = {}
    for inv in rules:
        title = inv.get("title")
        if not title:
            continue
        tid = rule_type_id(title)
        if tid in out:  # одно правило в нескольких сценариях: самый строгий статус, процедуры всех сценариев
            if severity(inv) == "violation":
                out[tid]["kind"] = "violation"
            seen = {pr["id"] for pr in out[tid]["procedures"]}
            out[tid]["procedures"] += [pr for pr in procedures(inv, topics) if pr["id"] not in seen]
            continue
        action = inv.get("action") or inv.get("then") or {}
        name = action if isinstance(action, str) else action.get("action") if isinstance(action, dict) else None
        out[tid] = {"id": tid, "title": title, "description": rule_description(inv, norms),
                    "observable": vocab.get(name, "") if name else "", "kind": severity(inv), "custom": False,
                    "status": "published", "samples": [], "norm": inv.get("norm"), "procedures": procedures(inv, topics)}
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
    if event == "sop_step":  # SOP-чекер: название называет шаг («Пропущен шаг: …», «Не по порядку: …»)
        return SOP_TYPES["Пропущен шаг процедуры"] if (title or "").startswith("Пропущен") else SOP_TYPES["Шаг процедуры не по порядку"]
    if title in SOP_TYPES:
        return SOP_TYPES[title]
    if status != "expected" and title:
        return rule_type_id(title)
    return f"step:{event}"
