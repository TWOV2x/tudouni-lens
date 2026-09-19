#!/usr/bin/env python3
"""土豆泥图视 CLI. Only talks to tudouni-api.com. Not a generic relay client."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# 土豆泥图视. 禁止改作其他中转站客户端。
PRODUCT = "土豆泥图视"
_APEX_SHA = "0d0b2ad41cae4a53cd1dd166b8864090d0cfc2ae278bb905aae1a2f5b7030a48"
_SKILL_SHA = "47d1cc27c7d309a9f7c7f3c749d1b703ec8345926673eb65d47f6fb2667ab945"
_PROD_SHA = "4fad492f40f195ab9a8236c6a39b7f3bc01a22c4e1df77ed933bdcf84e77b893"

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "references" / "catalog.json"
LEDGER_DIR = Path(".tudouni")
LEDGER_PATH = LEDGER_DIR / "jobs.jsonl"
LOCK_PATH = Path("tudouni.lock.json")
OUT_DIR = Path("output") / "tudouni"
KEYBOX_DIR = Path.home() / ".tudouni"
KEY_FILE = KEYBOX_DIR / "api_key"
BASE_FILE = KEYBOX_DIR / "base_url"
KEYBOX_HINT = str(KEY_FILE)


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def find_model(catalog: dict, model_id: str) -> dict | None:
    for row in catalog.get("images", []) + catalog.get("videos", []):
        if row.get("id") == model_id:
            return row
    return None


def die(msg: str, code: int = 2) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), file=sys.stderr)
    raise SystemExit(code)


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return key[:4] + "…" + key[-4:]


def read_keybox_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def write_keybox_file(path: Path, value: str) -> None:
    KEYBOX_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _skill_id() -> str:
    return "".join(("tudo", "uni-", "lens"))


def _brand_lock() -> None:
    """改品牌时只动：PRODUCT、_skill_id 拼法、文件头三枚 SHA、SKILL.md 的 name。"""
    if hashlib.sha256(PRODUCT.encode("utf-8")).hexdigest() != _PROD_SHA:
        die(PRODUCT + " 品牌校验失败")
    sid = _skill_id()
    if hashlib.sha256(sid.encode("ascii")).hexdigest() != _SKILL_SHA:
        die(PRODUCT + " 品牌校验失败")
    skill = ROOT / "SKILL.md"
    if not skill.is_file():
        die(PRODUCT + " 缺少 SKILL.md")
    head = skill.read_text(encoding="utf-8")[:800]
    if f"name: {sid}" not in head or PRODUCT not in head:
        die(PRODUCT + " 品牌校验失败")


def _apex() -> str:
    return "".join(("tud", "ouni", "-ap", "i.c", "om"))


def origin_host(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or "").lower()


def _ours(host: str) -> bool:
    apex = _apex()
    if hashlib.sha256(apex.encode("ascii")).hexdigest() != _APEX_SHA:
        return False
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    if h == apex:
        return hashlib.sha256(h.encode("ascii")).hexdigest() == _APEX_SHA
    return h.endswith("." + apex)


def _gate(url: str) -> None:
    if not _ours(origin_host(url)):
        die(PRODUCT + " 只连接官方站点，不能用于其他中转")


def normalize_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def assert_tudouni_origin(base: str) -> str:
    base = normalize_base(base) or ("https://" + _apex())
    if not base.startswith("https://"):
        die(PRODUCT + " 只连接官方站点")
    _gate(base)
    return base


def resolve_key(ns: argparse.Namespace | None = None) -> str:
    cli = getattr(ns, "api_key", None) if ns is not None else None
    return (
        (cli or "").strip()
        or read_keybox_file(KEY_FILE)
        or (os.environ.get("TUDOUNI_API_KEY") or "").strip()
        or (os.environ.get("OPENAI_API_KEY") or "").strip()
    )


def resolve_base(ns: argparse.Namespace | None = None) -> str:
    catalog = load_catalog()
    cli = getattr(ns, "base_url", None) if ns is not None else None
    raw = (
        (cli or "").strip()
        or read_keybox_file(BASE_FILE)
        or (os.environ.get("TUDOUNI_BASE_URL") or "").strip()
        or catalog.get("base_url_default")
        or ("https://" + _apex())
    )
    return assert_tudouni_origin(raw)


def auth(ns: argparse.Namespace) -> tuple[str, str]:
    key = resolve_key(ns)
    base = resolve_base(ns)
    if not key and not ns.dry_run:
        die(
            "还没有 Key。去 tudouni-api.com 开一把，写入 "
            + KEYBOX_HINT
            + "（一行，不要发到聊天里）。换 Key 改这个文件即可，不用关 Claude Code / Codex。"
        )
    return key, base


def key_rejected_message(detail: str) -> str:
    return (
        "这把 Key 站上不认。换 Key 不用关软件：把新 Key 单独一行写入 "
        + KEYBOX_HINT
        + " 后重试。不要发到聊天里。"
        + ((" 详情: " + detail[:200]) if detail else "")
    )


def hold_guard(catalog: dict, model: str) -> None:
    held = {x.lower() for x in catalog.get("hold", [])}
    if model.lower() in held:
        die("这款还没对你这把 Key 开")


def classify_media(model_id: str) -> str:
    low = (model_id or "").strip().lower()
    if not low:
        return "text"
    if low.startswith("gpt-image") or any(
        x in low for x in ("image", "banana", "dall-e", "dalle", "flux", "seedream", "imagen")
    ):
        return "image"
    if any(x in low for x in ("audio", "tts", "music", "suno", "whisper", "asr", "speech")):
        return "audio"
    if (
        any(x in low for x in ("video", "seedance", "kling", "h3video", "sora"))
        or low.startswith("sd2")
        or "wan" in low
        or low.startswith("minimax-h3")
    ):
        if any(x in low for x in ("chat", "instruct", "text-only")):
            return "text"
        return "video"
    return "text"


def image_tier(model_id: str) -> str:
    low = model_id.lower()
    if "4k" in low:
        return "4k"
    if "2k" in low:
        return "2k"
    if "1k" in low:
        return "1k"
    return "other"


def media_guard(model: str, expect: str | None = None) -> None:
    kind = classify_media(model)
    if kind == "text":
        die("土豆泥图视只出图、视频和音频，不用文字模型")
    if expect and kind != expect:
        die(f"{model} 是{kind}，这条命令只接受{expect}")


def parse_live_ids(payload: dict) -> list[str]:
    blob = payload.get("data") or payload.get("models") or payload
    names: list[str] = []
    if isinstance(blob, list):
        for item in blob:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("model") or item.get("name")
                if mid:
                    names.append(str(mid))
            elif item:
                names.append(str(item))
    return names


def bucket_media(ids: list[str]) -> dict:
    images = {"1k": [], "2k": [], "4k": [], "other": []}
    videos: list[str] = []
    audio: list[str] = []
    text = 0
    for mid in ids:
        kind = classify_media(mid)
        if kind == "image":
            images[image_tier(mid)].append(mid)
        elif kind == "video":
            videos.append(mid)
        elif kind == "audio":
            audio.append(mid)
        else:
            text += 1
    return {"images": images, "videos": videos, "audio": audio, "ignored_text": text}


def pick_default_image(ids: list[str]) -> str:
    images = [m for m in ids if classify_media(m) == "image"]
    ones = [m for m in images if image_tier(m) == "1k"]
    if ones:
        return ones[0]
    if images:
        return images[0]
    return "gpt-image-2-1k"


def family_stem(mid: str) -> str:
    low = mid.lower()
    for suf in ("-sunburst-4k", "-sunburst-2k", "-sunburst-1k", "-4k", "-2k", "-1k"):
        if low.endswith(suf):
            return low[: -len(suf)]
    return low


def infer_tier(text: str | None) -> str | None:
    low = (text or "").lower()
    if "4k" in low:
        return "4k"
    if "2k" in low:
        return "2k"
    if "1k" in low:
        return "1k"
    return None


def normalize_image_name(name: str) -> str:
    n = name.strip().lower().replace(" ", "")
    n = n.replace("gptimage", "gpt-image")
    if n.startswith("image-") or n.startswith("image2"):
        n = "gpt-" + n if not n.startswith("gpt-") else n
    if n in {"2.5", "image2.5", "image-2.5"}:
        n = "gpt-image-2.5"
    if n in {"image-2", "image2"}:
        n = "gpt-image-2"
    return n


def resolve_image_model(requested: str | None, tier: str | None, ids: list[str]) -> tuple[str, str | None]:
    images = [m for m in ids if classify_media(m) == "image"]
    want = (tier or "").lower() or infer_tier(requested)
    if want not in {"1k", "2k", "4k"}:
        want = None
    if requested:
        req = requested.strip()
        exact = [m for m in images if m.lower() == req.lower()]
        if exact and (not want or image_tier(exact[0]) == want):
            return exact[0], None
        stem = family_stem(normalize_image_name(req))
        family = [m for m in images if family_stem(m) == stem or stem in m.lower()]
        if want:
            hit = [m for m in family if image_tier(m) == want]
            if hit:
                note = None if hit[0].lower() == req.lower() else f"{req} → {hit[0]}"
                return hit[0], note
        if family:
            ones = [m for m in family if image_tier(m) == "1k"]
            pick = ones[0] if ones else family[0]
            return pick, (None if pick.lower() == req.lower() else f"{req} → {pick}")
    if want:
        hit = [m for m in images if image_tier(m) == want]
        if hit:
            return hit[0], None
    return pick_default_image(ids), None


def speak_shelf(buckets: dict, source: str) -> str:
    img = buckets["images"]

    def join(xs: list[str]) -> str:
        return "、".join(xs) if xs else "暂无"

    lines = [
        "图 1K（草稿）：" + join(img["1k"]),
        "图 2K（常用）：" + join(img["2k"]),
        "图 4K（更细）：" + join(img["4k"]),
    ]
    if img["other"]:
        lines.append("图 其它：" + join(img["other"]))
    if buckets["videos"]:
        lines.append("视频：" + join(buckets["videos"]))
    if buckets["audio"]:
        lines.append("音频：" + join(buckets["audio"]))
    if source != "live":
        lines.append("货盘是离线兜底，活目录没拉到。")
    return "\n".join(lines)


INVOKE_MENU = (
    "下次直接说：出一张… / 出一条视频…；或再打 /tudouni-lens（Codex 用 $tudouni-lens）。"
    "看货盘说「我能出什么」。"
)


def collect_media_ids(ns: argparse.Namespace) -> tuple[list[str], str]:
    catalog = load_catalog()
    key = resolve_key(ns)
    base = resolve_base(ns)
    if key:
        data = http_json("GET", f"{base}/v1/models", key, None, fail=False)
        if data.get("_http_error") == 401 or "Invalid API Key" in str(data.get("_body") or ""):
            die(key_rejected_message(str(data.get("_body") or "")))
        if not data.get("_http_error"):
            return parse_live_ids(data), "live"
    fallback = [x["id"] for x in catalog.get("images", []) + catalog.get("videos", []) + catalog.get("audio", [])]
    return fallback, "fallback"


def load_lock() -> dict:
    if not LOCK_PATH.exists():
        return {"palette": [], "avoid": [], "refs": [], "last_output": None}
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def save_lock(data: dict) -> None:
    LOCK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_ledger(row: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    row = dict(row)
    row.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_ledger(limit: int = 20) -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def http_json(
    method: str,
    url: str,
    key: str,
    body: dict | None = None,
    timeout: int = 120,
    fail: bool = True,
) -> dict:
    _gate(url)
    data = None
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "X-Tudouni-Lens": "1",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        if not fail:
            return {"_http_error": exc.code, "_body": detail, "_url": url}
        if exc.code == 401 or "Invalid API Key" in detail:
            die(key_rejected_message(detail))
        die(f"HTTP {exc.code} {url}: {detail}")
    except urllib.error.URLError as exc:
        if not fail:
            return {"_http_error": 0, "_body": str(exc.reason), "_url": url}
        die(f"network error {url}: {exc.reason}")
    return {}


def http_download(url: str, dest: Path, key: str | None = None) -> Path:
    _gate(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as fh:
        fh.write(resp.read())
    return dest


def ratio_to_size(ratio: str) -> str:
    mapping = {"1:1": "1024x1024", "16:9": "1536x1024", "9:16": "1024x1536"}
    return mapping.get(ratio, "1024x1024")


def compose_spec(ns: argparse.Namespace) -> dict:
    lock = load_lock()
    avoid = list(lock.get("avoid") or [])
    if ns.avoid:
        avoid.extend(x.strip() for x in ns.avoid.split(";") if x.strip())
    refs = list(lock.get("refs") or [])
    if ns.ref:
        refs.append({"path": ns.ref, "role": ns.ref_role or "风格"})
    spec = {
        "用途": ns.use or "",
        "画幅": ns.ratio or "1:1",
        "主体": ns.subject or ns.prompt or "",
        "机位/构图": ns.framing or "",
        "光线/材质": ns.lighting or "",
        "必须留下": ns.keep or "",
        "不准出现": "；".join(avoid),
        "参考图角色": refs,
        "模型": ns.model or "",
        "时长/秒": ns.duration,
        "origin_prompt": ns.prompt or "",
    }
    return spec


def dialect_prompt(spec: dict) -> str:
    parts = []
    if spec.get("origin_prompt"):
        parts.append(str(spec["origin_prompt"]))
    for key in ("用途", "画幅", "主体", "机位/构图", "光线/材质", "必须留下", "不准出现"):
        val = spec.get(key)
        if val:
            parts.append(f"{key}: {val}")
    return "\n".join(parts)


def quote_model(catalog: dict, model: str, duration: int | None) -> dict:
    media_guard(model)
    hold_guard(catalog, model)
    plaza = catalog.get("plaza") or catalog.get("base_url_default") or "https://tudouni-api.com"
    note = catalog.get("pricing_note") or "登录 tudouni-api.com 打开模型广场查看现价"
    row = find_model(catalog, model)
    if row is None:
        return {
            "ok": True,
            "model": model,
            "pricing": note,
            "plaza": plaza,
            "note": "不在推荐目录。现价以模型广场为准。",
        }
    if row.get("kind") == "image":
        return {
            "ok": True,
            "model": model,
            "kind": "image",
            "pricing": note,
            "plaza": plaza,
        }
    seconds = duration
    if row.get("seconds_lock"):
        seconds = int(row["seconds_lock"])
    if seconds is None:
        seconds = int(row.get("seconds_default") or 5)
    return {
        "ok": True,
        "model": model,
        "kind": "video",
        "seconds": seconds,
        "resolution": row.get("resolution"),
        "pricing": note,
        "plaza": plaza,
    }


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_compose(ns: argparse.Namespace) -> None:
    spec = compose_spec(ns)
    emit({"ok": True, "spec": spec, "dialect_prompt": dialect_prompt(spec)})


def cmd_models(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    ids, source = collect_media_ids(ns)
    buckets = bucket_media(ids)
    emit(
        {
            "ok": True,
            "source": source,
            "pricing": catalog.get("pricing_note"),
            "plaza": catalog.get("plaza") or "https://tudouni-api.com",
            "images": buckets["images"],
            "videos": buckets["videos"],
            "audio": buckets["audio"],
            "ignored_text": buckets["ignored_text"],
            "speak": speak_shelf(buckets, source),
            "note": "只列出图、视频、音频。文字模型已过滤。现价看模型广场。",
        }
    )


def cmd_doctor(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    key = resolve_key(ns)
    base = resolve_base(ns)
    payload = {
        "ok": True,
        "ready": False,
        "keybox": KEY_FILE.exists(),
        "key": mask_key(key) if key else "",
        "base_url": base,
        "plaza": catalog.get("plaza") or "https://tudouni-api.com",
        "keybox_path": KEYBOX_HINT,
    }
    if not key:
        payload["speak"] = (
            "还没接上 Key。去 tudouni-api.com 开一把，单独一行写入 "
            + KEYBOX_HINT
            + "。写完下一句就能出，不用关软件。不要发到聊天里。现价在网站模型广场。\n"
            + INVOKE_MENU
        )
        emit(payload)
        return
    ids, source = collect_media_ids(ns)
    buckets = bucket_media(ids)
    payload["ready"] = source == "live"
    payload["source"] = source
    payload["images"] = buckets["images"]
    payload["videos"] = buckets["videos"]
    payload["audio"] = buckets["audio"]
    payload["ignored_text"] = buckets["ignored_text"]
    payload["speak"] = (
        "土豆泥图视已接上。\n"
        + speak_shelf(buckets, source)
        + "\n现价去 tudouni-api.com 模型广场看一眼就行，后面出图不再念价。\n"
        + INVOKE_MENU
    )
    emit(payload)


def cmd_quote(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    if not ns.model:
        die("quote requires --model")
    emit(quote_model(catalog, ns.model, ns.duration))


def cmd_generate(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    key, base = auth(ns)
    ids, _source = collect_media_ids(ns)
    if ns.model:
        kind = classify_media(ns.model)
        kind2 = classify_media(normalize_image_name(ns.model))
        if kind != "image" and kind2 != "image":
            media_guard(ns.model, "image")
    model, mapped = resolve_image_model(ns.model, getattr(ns, "tier", None), ids)
    media_guard(model, "image")
    hold_guard(catalog, model)
    spec = compose_spec(ns)
    prompt = dialect_prompt(spec)
    size = ns.size or ratio_to_size(ns.ratio or "1:1")
    body = {"model": model, "prompt": prompt, "size": size, "n": 1, "response_format": "url"}
    out = Path(ns.out) if ns.out else OUT_DIR / f"image-{int(time.time())}.png"
    if ns.dry_run:
        emit(
            {
                "ok": True,
                "dry_run": True,
                "url": f"{base}/v1/images/generations",
                "body": body,
                "out": str(out),
                "mapped": mapped,
            }
        )
        return
    data = http_json("POST", f"{base}/v1/images/generations", key, body, fail=False)
    if data.get("_http_error") == 401 or "Invalid API Key" in str(data.get("_body") or ""):
        die(key_rejected_message(str(data.get("_body") or "")))
    if data.get("_http_error") == 403 and "not allowed" in str(data.get("_body") or "").lower():
        alt = pick_default_image(ids)
        if alt != model:
            mapped = f"{model} 这把 Key 不能用，改走 {alt}"
            model = alt
            body["model"] = model
            data = http_json("POST", f"{base}/v1/images/generations", key, body, fail=False)
    if data.get("_http_error"):
        die(f"HTTP {data.get('_http_error')} {data.get('_url')}: {data.get('_body')}")
    item = ((data.get("data") or [{}])[0]) if isinstance(data.get("data"), list) else {}
    url = item.get("url")
    b64 = item.get("b64_json")
    saved = None
    if url:
        saved = str(http_download(url, out))
    elif b64:
        import base64

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(b64))
        saved = str(out)
    else:
        die("image response missing url/b64_json")
    lock = load_lock()
    lock["last_output"] = saved
    save_lock(lock)
    append_ledger({"cmd": "generate", "model": model, "status": "completed", "out": saved, "mapped": mapped})
    emit({"ok": True, "model": model, "out": saved, "path": saved, "mapped": mapped})


def cmd_edit(ns: argparse.Namespace) -> None:
    if not ns.image:
        die("edit requires --image")
    catalog = load_catalog()
    key, base = auth(ns)
    ids, _source = collect_media_ids(ns)
    model, _mapped = resolve_image_model(ns.model, getattr(ns, "tier", None), ids)
    media_guard(model, "image")
    hold_guard(catalog, model)
    spec = compose_spec(ns)
    prompt = dialect_prompt(spec)
    image_path = Path(ns.image)
    if not image_path.exists():
        die(f"image not found: {image_path}")
    import base64

    payload = {
        "model": model,
        "prompt": prompt,
        "image": "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode("ascii"),
    }
    out = Path(ns.out) if ns.out else OUT_DIR / f"edit-{int(time.time())}.png"
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "url": f"{base}/v1/images/edits", "out": str(out), "image": str(image_path)})
        return
    data = http_json("POST", f"{base}/v1/images/edits", key, payload)
    item = ((data.get("data") or [{}])[0]) if isinstance(data.get("data"), list) else {}
    url = item.get("url")
    if not url:
        die("edit response missing url")
    saved = str(http_download(url, out))
    lock = load_lock()
    lock["last_output"] = saved
    save_lock(lock)
    append_ledger({"cmd": "edit", "model": model, "status": "completed", "out": saved})
    emit({"ok": True, "model": model, "out": saved, "path": saved})


def apply_video_locks(row: dict | None, duration: int | None, resolution: str | None) -> tuple[int, str]:
    seconds = duration
    reso = resolution
    if row:
        if row.get("seconds_lock"):
            seconds = int(row["seconds_lock"])
        elif seconds is None:
            seconds = int(row.get("seconds_default") or 5)
        if row.get("lock") and row.get("resolution"):
            reso = row["resolution"]
        elif not reso:
            reso = row.get("resolution") or "720p"
    if seconds is None:
        seconds = 5
    if not reso:
        reso = "720p"
    return seconds, reso


def cmd_submit_video(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    key, base = auth(ns)
    model = ns.model
    if not model:
        die("submit-video requires --model")
    media_guard(model, "video")
    hold_guard(catalog, model)
    row = find_model(catalog, model)
    if not ns.ratio:
        ns.ratio = "16:9"
    seconds, reso = apply_video_locks(row, ns.duration, ns.resolution)
    quoted = quote_model(catalog, model, seconds)
    if not ns.yes:
        emit({"ok": False, "error": "video submit refused without --yes", "quote": quoted})
        raise SystemExit(2)
    spec = compose_spec(ns)
    body = {
        "model": model,
        "prompt": dialect_prompt(spec),
        "duration": seconds,
        "ratio": ns.ratio or "16:9",
        "resolution": reso,
    }
    if ns.ref:
        body["images"] = [ns.ref]
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "url": f"{base}/v1/video/generations", "body": body, "quote": quoted})
        return
    data = http_json("POST", f"{base}/v1/video/generations", key, body)
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        die(f"submit missing task_id: {json.dumps(data, ensure_ascii=False)[:400]}")
    append_ledger(
        {
            "cmd": "submit-video",
            "model": model,
            "task_id": task_id,
            "status": data.get("status") or "queued",
            "pricing": quoted.get("pricing"),
            "plaza": quoted.get("plaza"),
        }
    )
    emit({"ok": True, "task_id": task_id, "status": data.get("status") or "queued", "quote": quoted, "path": None})


def normalize_status(raw: str | None) -> str:
    value = (raw or "").lower()
    if value in {"pending", "queued", "submitted"}:
        return "queued"
    if value in {"in_progress", "processing", "running"}:
        return "in_progress"
    if value in {"completed", "success", "succeeded"}:
        return "completed"
    if value in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    return value or "unknown"


def poll_once(key: str, base: str, task_id: str) -> dict:
    primary = f"{base}/v1/video/generations/{task_id}"
    data = http_json("GET", primary, key, None, fail=False)
    if not data.get("_http_error"):
        return data
    if data.get("_http_error") in {404, 405}:
        fallback = http_json("GET", f"{base}/v1/tasks/{task_id}", key, None, fail=False)
        if not fallback.get("_http_error"):
            return fallback
        die(f"HTTP {fallback.get('_http_error')} poll fallback: {fallback.get('_body')}")
    die(f"HTTP {data.get('_http_error')} {primary}: {data.get('_body')}")
    return {}


def cmd_poll(ns: argparse.Namespace) -> None:
    key, base = auth(ns)
    task_id = ns.task_id
    if not task_id:
        die("poll requires --task-id")
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "url": f"{base}/v1/video/generations/{task_id}"})
        return
    data = poll_once(key, base, task_id)
    status = normalize_status(data.get("status"))
    append_ledger({"cmd": "poll", "task_id": task_id, "status": status})
    emit({"ok": True, "task_id": task_id, "status": status, "raw_status": data.get("status"), "data": data.get("data")})


def extract_url(data: dict) -> str | None:
    blob = data.get("data")
    if isinstance(blob, list) and blob:
        first = blob[0]
        if isinstance(first, dict):
            return first.get("url") or first.get("video_url")
        if isinstance(first, str):
            return first
    if isinstance(blob, dict):
        return blob.get("url") or blob.get("video_url")
    return data.get("url")


def cmd_download(ns: argparse.Namespace) -> None:
    key, base = auth(ns)
    url = ns.url
    task_id = ns.task_id
    out = Path(ns.out) if ns.out else OUT_DIR / f"video-{int(time.time())}.mp4"
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "task_id": task_id, "out": str(out)})
        return
    if not url:
        if not task_id:
            die("download requires --url or --task-id")
        data = poll_once(key, base, task_id)
        if normalize_status(data.get("status")) != "completed":
            die(f"task not completed: {data.get('status')}")
        url = extract_url(data)
        if not url:
            die("completed task missing url")
    saved = str(http_download(url, out, key=None))
    lock = load_lock()
    lock["last_output"] = saved
    save_lock(lock)
    append_ledger({"cmd": "download", "task_id": task_id, "status": "completed", "out": saved})
    emit({"ok": True, "out": saved, "path": saved, "task_id": task_id})


def cmd_jobs(ns: argparse.Namespace) -> None:
    emit({"ok": True, "jobs": read_ledger(ns.limit or 20), "ledger": str(LEDGER_PATH)})


def cmd_lock(ns: argparse.Namespace) -> None:
    lock = load_lock()
    if ns.avoid:
        lock["avoid"] = [x.strip() for x in ns.avoid.split(";") if x.strip()]
    if ns.ref:
        refs = list(lock.get("refs") or [])
        refs.append({"path": ns.ref, "role": ns.ref_role or "风格"})
        lock["refs"] = refs
        save_lock(lock)
    elif ns.avoid:
        save_lock(lock)
    emit({"ok": True, "lock": lock, "path": str(LOCK_PATH)})


def cmd_config(ns: argparse.Namespace) -> None:
    if ns.clear:
        if KEY_FILE.exists():
            KEY_FILE.unlink()
        emit({"ok": True, "cleared": True, "path": KEYBOX_HINT})
        return
    if ns.set_key_file:
        src = Path(ns.set_key_file)
        if not src.exists():
            die("key file not found")
        write_keybox_file(KEY_FILE, src.read_text(encoding="utf-8"))
        emit({"ok": True, "saved": True, "path": KEYBOX_HINT, "key": mask_key(read_keybox_file(KEY_FILE))})
        return
    if ns.set_key_stdin:
        raw = sys.stdin.read()
        if not raw.strip():
            die("stdin empty")
        write_keybox_file(KEY_FILE, raw)
        emit({"ok": True, "saved": True, "path": KEYBOX_HINT, "key": mask_key(read_keybox_file(KEY_FILE))})
        return
    if ns.set_base:
        write_keybox_file(BASE_FILE, assert_tudouni_origin(ns.set_base))
        emit({"ok": True, "base_url": resolve_base(ns), "path": str(BASE_FILE)})
        return
    key = resolve_key(ns)
    emit(
        {
            "ok": True,
            "path": KEYBOX_HINT,
            "key": mask_key(key) if key else "",
            "has_keybox": KEY_FILE.exists(),
            "has_env": bool(os.environ.get("TUDOUNI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
            "base_url": resolve_base(ns),
            "note": "钥匙盒优先于环境变量。换 Key 改文件即可，不用关 Claude Code / Codex。",
        }
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tudouni", description="tudouni-media CLI")
    p.add_argument("--base-url")
    p.add_argument("--api-key")
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_compose_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--prompt")
        sp.add_argument("--model")
        sp.add_argument("--use")
        sp.add_argument("--ratio")
        sp.add_argument("--subject")
        sp.add_argument("--framing")
        sp.add_argument("--lighting")
        sp.add_argument("--keep")
        sp.add_argument("--avoid")
        sp.add_argument("--ref")
        sp.add_argument("--ref-role")
        sp.add_argument("--duration", type=int)
        sp.add_argument("--size")
        sp.add_argument("--tier", choices=["1k", "2k", "4k"])
        sp.add_argument("--out")

    sp = sub.add_parser("compose")
    add_compose_flags(sp)
    sub.add_parser("models")
    sub.add_parser("doctor")
    sp = sub.add_parser("quote")
    sp.add_argument("--model", required=True)
    sp.add_argument("--duration", type=int)
    sp = sub.add_parser("generate")
    add_compose_flags(sp)
    sp = sub.add_parser("edit")
    add_compose_flags(sp)
    sp.add_argument("--image")
    sp = sub.add_parser("submit-video")
    add_compose_flags(sp)
    sp.add_argument("--resolution")
    sp.add_argument("--yes", action="store_true")
    sp = sub.add_parser("poll")
    sp.add_argument("--task-id", required=True)
    sp = sub.add_parser("download")
    sp.add_argument("--task-id")
    sp.add_argument("--url")
    sp.add_argument("--out")
    sp = sub.add_parser("jobs")
    sp.add_argument("--limit", type=int, default=20)
    sp = sub.add_parser("lock")
    sp.add_argument("--avoid")
    sp.add_argument("--ref")
    sp.add_argument("--ref-role")
    sp = sub.add_parser("config")
    sp.add_argument("--set-key-file")
    sp.add_argument("--set-key-stdin", action="store_true")
    sp.add_argument("--set-base")
    sp.add_argument("--clear", action="store_true")
    return p


def peel_globals(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    g = argparse.Namespace(dry_run=False, base_url=None, api_key=None)
    kept: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            g.dry_run = True
        elif a == "--base-url" and i + 1 < len(argv):
            i += 1
            g.base_url = argv[i]
        elif a.startswith("--base-url="):
            g.base_url = a.split("=", 1)[1]
        elif a == "--api-key" and i + 1 < len(argv):
            i += 1
            g.api_key = argv[i]
        elif a.startswith("--api-key="):
            g.api_key = a.split("=", 1)[1]
        else:
            kept.append(a)
        i += 1
    return g, kept


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raw = list(argv) if argv is not None else sys.argv[1:]
    g, kept = peel_globals(raw)
    ns = build_parser().parse_args(kept)
    if g.dry_run:
        ns.dry_run = True
    if g.base_url:
        ns.base_url = g.base_url
    if g.api_key:
        ns.api_key = g.api_key
    _brand_lock()
    dispatch = {
        "compose": cmd_compose,
        "models": cmd_models,
        "doctor": cmd_doctor,
        "quote": cmd_quote,
        "generate": cmd_generate,
        "edit": cmd_edit,
        "submit-video": cmd_submit_video,
        "poll": cmd_poll,
        "download": cmd_download,
        "jobs": cmd_jobs,
        "lock": cmd_lock,
        "config": cmd_config,
    }
    dispatch[ns.cmd](ns)


if __name__ == "__main__":
    main()
