#!/usr/bin/env python3
"""土豆泥图视 CLI. Only talks to tudouni-api.com. Not a generic relay client."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote

# 土豆泥图视. 禁止改作其他中转站客户端。
PRODUCT = "土豆泥图视"
_APEX_SHA = "0d0b2ad41cae4a53cd1dd166b8864090d0cfc2ae278bb905aae1a2f5b7030a48"
_SKILL_SHA = "47d1cc27c7d309a9f7c7f3c749d1b703ec8345926673eb65d47f6fb2667ab945"
_PROD_SHA = "4fad492f40f195ab9a8236c6a39b7f3bc01a22c4e1df77ed933bdcf84e77b893"
PUBLISHER = "土豆泥"
_PUBLISHER_SHA = "475d280a300ff73497e641e136c05c36f0354bdc73546bd89fd2a91a80e022c8"
_LOGO_SHA = "609b1f000f7ffc7617c8b5f60ba19de47059564af9f8e73b35ddeea3a6cfd25d"
# Release layout is explicit: deleting plugin manifests must not silently turn
# a plugin into a standalone skill. Only the official standalone packer changes both values.
_DISTRIBUTION_MODE = "plugin"
_DISTRIBUTION_SHA = "5e689e2b01672bf33996e75d5e372ff60c536ce1599a1458e867cd8f4bef5160"

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
PROJECT_DIR: Path | None = None
LIVE_MODEL_TYPES: dict[str, str] = {}


def project_dir() -> Path:
    return PROJECT_DIR or Path.cwd()


def output_path(value: str | None, kind: str, suffix: str) -> Path:
    path=Path(value).expanduser() if value else OUT_DIR/(kind+'-'+uuid.uuid4().hex[:12]+suffix)
    return (path if path.is_absolute() else project_dir()/path).resolve()


def media_suffix(prefix: bytes) -> str | None:
    return '.jpg' if prefix.startswith(b'\xff\xd8\xff') else '.png' if prefix.startswith(b'\x89PNG\r\n\x1a\n') else '.webp' if prefix.startswith(b'RIFF') and prefix[8:12]==b'WEBP' else '.gif' if prefix.startswith((b'GIF87a',b'GIF89a')) else '.mp4' if prefix[4:8] in (b'ftyp',b'moov',b'mdat',b'styp') else '.webm' if prefix.startswith(b'\x1aE\xdf\xa3') else None


def actual_media_path(dest: Path,suffix: str) -> Path:
    actual=dest.with_suffix(suffix)
    if actual!=dest and actual.exists():actual=actual.with_name(actual.stem+'-'+uuid.uuid4().hex[:8]+suffix)
    return actual


def save_inline_image(value: str,dest: Path) -> Path:
    import base64
    data=base64.b64decode(value,validate=True);suffix=media_suffix(data[:32])
    if suffix not in ('.png','.jpg','.webp','.gif'):raise ValueError('响应里的图片格式不可识别')
    dest=actual_media_path(dest.resolve(),suffix);dest.parent.mkdir(parents=True,exist_ok=True)
    temporary=dest.with_name(dest.name+'.part-'+uuid.uuid4().hex[:8]);temporary.write_bytes(data);os.replace(temporary,dest);return dest


def configure_workspace(path: str | None) -> None:
    global PROJECT_DIR, LEDGER_DIR, LEDGER_PATH, LOCK_PATH, OUT_DIR
    PROJECT_DIR = Path(path).expanduser().resolve() if path else Path.cwd().resolve()
    if not PROJECT_DIR.is_dir():
        die('项目目录不存在，请选择客户当前项目')
    LEDGER_DIR = PROJECT_DIR / '.tudouni'
    LEDGER_PATH = LEDGER_DIR / 'jobs.jsonl'
    LOCK_PATH = PROJECT_DIR / 'tudouni.lock.json'
    OUT_DIR = PROJECT_DIR / 'output' / 'tudouni'


def local_key_file() -> Path:
    return project_dir() / ".tudouni" / "api_key"


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
    try:
        raw = path.read_bytes()
        encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        return raw.decode(encoding).strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as error:
        die(f'本机凭据读取失败（{type(error).__name__}）。请由助手打开配置窗口修复；不需要客户移动 Key 文件或操作终端。')


def write_keybox_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex[:8])
    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as handle:
        handle.write(value.strip() + '\n')
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_key(value: str) -> Path:
    dest = local_key_file()
    write_keybox_file(dest, validate_key(value))
    ignore = dest.parent / '.gitignore'
    if not ignore.exists():
        write_keybox_file(ignore, '*')
    return dest


def keybox_paths() -> list[Path]:
    home = KEY_FILE
    local = local_key_file()
    if local.resolve() == home.resolve():
        return [home]
    return [local, home]


def _skill_id() -> str:
    return "".join(("tudo", "uni-", "lens"))


def _brand_lock() -> None:
    """Local brand consistency only; no activation service, customer data or destructive action.

    Maintenance/antidote: project 锁魂焚诀秘籍, plugin chapter. Versions, copywriting,
    caches and model menus may evolve without changing this identity contract.
    """
    def reject(field):
        die(PRODUCT + ' 安装包不完整或已被改动，请重新安装官方完整包。检查项：' + field)

    def scalar(raw):
        raw = raw.strip()
        if raw.startswith('"'):
            try:
                value, end = json.JSONDecoder().raw_decode(raw)
                tail = raw[end:].strip()
                if not isinstance(value, str) or (tail and not tail.startswith('#')):
                    raise ValueError()
                return value
            except (ValueError, TypeError):
                reject('metadata_scalar')
        if raw.startswith("'"):
            match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
            if not match: reject('metadata_scalar')
            return match.group(1).replace("''", "'")
        return raw.split(' #', 1)[0].strip()

    def yaml_fields(text, wanted, interface=False):
        # Protected metadata is emitted as simple scalars by our release builder.
        # Reject duplicate/ambiguous protected keys instead of guessing YAML semantics.
        result, active, seen_interface = {}, not interface, False
        key_pattern = r'''("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)'''
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#'): continue
            if interface and not line[0].isspace():
                top = re.fullmatch(key_pattern, line)
                if not top: reject('metadata_mapping')
                if scalar(top.group(1)) == 'interface':
                    if seen_interface: reject('duplicate_interface')
                    if top.group(2).strip() and not top.group(2).lstrip().startswith('#'):
                        reject('interface_mapping')
                    seen_interface, active = True, True
                else:
                    active = False
                continue
            if not active: continue
            match = re.fullmatch(('  ' if interface else '') + key_pattern, line)
            if interface and not match: reject('interface_scalar')
            if match and scalar(match.group(1)) in wanted:
                key = scalar(match.group(1))
                if key in result: reject('duplicate_' + key)
                result[key] = scalar(match.group(2))
        return result

    def inside(base, value):
        if not isinstance(value, str) or not value or ':' in value or value.startswith(('/', '\\')):
            reject('asset_path')
        relative = value.replace('\\', '/')
        if '..' in relative.split('/'):
            reject('asset_path')
        target = (base / relative).resolve()
        if not target.is_relative_to(base.resolve()): reject('asset_path')
        return target

    def logo(base, value):
        target = inside(base, value)
        if not target.is_file() or target.stat().st_size > 4 * 1024 * 1024:
            reject('logo_file')
        if hashlib.sha256(target.read_bytes()).hexdigest() != _LOGO_SHA:
            reject('logo_identity')

    def manifest(path):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value: reject('duplicate_manifest_field')
                value[key] = item
            return value
        value = json.loads(path.read_text('utf-8-sig'), object_pairs_hook=unique)
        if not isinstance(value, dict): reject('manifest_shape')
        return value

    def version_base(value):
        if not isinstance(value, str) or not re.fullmatch(
                r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?', value):
            reject('release_version')
        return value.split('+', 1)[0]

    if hashlib.sha256(PRODUCT.encode("utf-8")).hexdigest() != _PROD_SHA:
        reject('product')
    sid = _skill_id()
    if hashlib.sha256(sid.encode("ascii")).hexdigest() != _SKILL_SHA:
        reject('skill_identity')
    if hashlib.sha256(PUBLISHER.encode('utf-8')).hexdigest() != _PUBLISHER_SHA:
        reject('publisher')
    if (_DISTRIBUTION_MODE not in ('plugin', 'standalone') or
            hashlib.sha256(_DISTRIBUTION_MODE.encode('ascii')).hexdigest() != _DISTRIBUTION_SHA):
        reject('distribution_identity')
    try:
        text = (ROOT / 'SKILL.md').read_text('utf-8-sig')
        front = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', text, re.S)
        if not front or yaml_fields(front.group(1), {'name'}).get('name') != sid:
            reject('skill_frontmatter')
        if not re.search(r'^#\s+' + re.escape(PRODUCT) + r'\s*$', text, re.M):
            reject('skill_title')
        ui = yaml_fields((ROOT / 'agents/openai.yaml').read_text('utf-8-sig'),
                         {'display_name', 'icon_small', 'icon_large'}, interface=True)
        if ui.get('display_name') != PRODUCT: reject('skill_display_name')
        for field in ('icon_small', 'icon_large'):
            logo(ROOT, ui.get(field))
        logo(ROOT, 'assets/tudouni-logo.png')  # The connection form uses this same asset.
        license_text = (ROOT / 'LICENSE').read_text('utf-8-sig')
        if PRODUCT not in license_text or _apex() not in license_text:
            reject('license_identity')
        package = ROOT.parent.parent
        outer_manifest_present = any((package / ('.' + engine + '-plugin') / 'plugin.json').exists()
                                     for engine in ('codex', 'claude'))
        # A genuine standalone export needs no outer files, but placing that
        # export inside a plugin must not bypass the actual plugin's identity.
        if _DISTRIBUTION_MODE == 'plugin' or outer_manifest_present:
            version = version_base((ROOT / 'VERSION').read_text('utf-8').strip())
            for engine in ('codex', 'claude'):
                item = manifest(package / ('.' + engine + '-plugin') / 'plugin.json')
                if item.get('name') != sid: reject(engine + '_plugin_name')
                if not isinstance(item.get('author'), dict) or item['author'].get('name') != PUBLISHER:
                    reject(engine + '_publisher')
                assert_tudouni_origin(item.get('homepage') or 'invalid')
                if version_base(item.get('version')) != version:
                    reject(engine + '_release_version')
                declared = item.get('skills')
                declared = [declared] if isinstance(declared, str) else declared
                if not isinstance(declared, list) or len(declared) != 1 or inside(package, declared[0]) != ROOT.parent.resolve():
                    reject(engine + '_skill_binding')
                if engine == 'codex':
                    card = item.get('interface')
                    if not isinstance(card, dict) or card.get('displayName') != PRODUCT or card.get('developerName') != PUBLISHER:
                        reject('plugin_display_name')
                    assert_tudouni_origin(card.get('websiteURL') or 'invalid')
                    for field in ('logo', 'logoDark', 'composerIcon'):
                        logo(package, card.get(field))
    except (OSError, ValueError, TypeError, AttributeError):
        reject('package_structure')


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
    try:
        parsed = urlparse(url)
        valid_transport = (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                           and not parsed.fragment and parsed.port in (None, 443))
    except (ValueError, TypeError):
        valid_transport = False
    if not valid_transport:
        die(PRODUCT + ' API 仅使用本站 HTTPS 接口，不接受明文连接、账号式URL或额外端口')
    if not _ours(parsed.hostname or ''):
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
    parsed = urlparse(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None,443):
        die('请填写本站 HTTPS 接口根地址，不带账号、查询参数、片段或额外端口')
    return base


def validate_key(value: str) -> str:
    value = value.strip().lstrip('\ufeff')
    if value.startswith('Bearer '):
        value = value[7:].strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{16,8192}', value):
        die('Key 格式不正确，请在配置窗口重新粘贴完整的 Key。')
    return value


def resolve_key(ns: argparse.Namespace | None = None) -> str:
    explicit = getattr(ns, 'key_file', None) if ns else None
    if explicit:
        value = read_keybox_file(Path(explicit).expanduser().resolve())
        if not value:
            die('尚未连接有效凭据，请由助手打开配置窗口；不需要客户创建文件。')
        return validate_key(value)
    boxed = ""
    for path in keybox_paths():
        boxed = read_keybox_file(path)
        if boxed:
            break
    value = boxed or (os.environ.get('TUDOUNI_API_KEY') or '').strip()
    # A generic OPENAI_API_KEY may belong to another vendor; never send it to this relay implicitly.
    return validate_key(value) if value else ''


def resolve_base(ns: argparse.Namespace | None = None) -> str:
    catalog = load_catalog()
    cli = getattr(ns, "base_url", None) if ns is not None else None
    raw = (
        (cli or "").strip()
        or read_keybox_file(project_dir() / '.tudouni' / 'base_url')
        or read_keybox_file(BASE_FILE)
        or (os.environ.get("TUDOUNI_BASE_URL") or "").strip()
        or catalog.get("base_url_default")
        or ("https://" + _apex())
    )
    return assert_tudouni_origin(raw)


def auth(ns: argparse.Namespace) -> tuple[str, str]:
    pinned = getattr(ns, '_operation_auth', None)
    if pinned:
        return pinned
    key = resolve_key(ns)
    base = resolve_base(ns)
    if not key and not ns.dry_run:
        from onboarding import start_setup
        emit(start_setup(sys.modules[__name__], ns))
        raise SystemExit(3)  # No paid request was submitted; continue after the local form is saved.
    ns._operation_auth = (key, base)
    return key, base


def key_rejected_message(detail: str) -> str:
    return (
        "这把 Key 未通过验证。请由助手运行 setup --replace 打开配置窗口，在同一窗口更新 Key；"
        "不需要客户创建文件、移动 Key 或操作终端。"
    )


def hold_guard(catalog: dict, model: str) -> None:
    held = {x.lower() for x in catalog.get("hold", [])}
    if model.lower() in held:
        die("网站已开放此型号，但当前客户端仍有兼容性限制，需要更新适配；不是 Key 保存位置问题。")


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
    kind = LIVE_MODEL_TYPES.get(model.casefold(), classify_media(model))
    if kind == 'unknown':
        die('网站尚未标明这个型号的图视类别，暂不猜测调用方式；请刷新菜单核对。')
    if kind == "text":
        die("土豆泥图视只处理图片和视频，不调用文字模型")
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
        if exact and (not want or image_tier(exact[0]) in (want,'other')):
            return exact[0], None
        stem = family_stem(normalize_image_name(req))
        family = [m for m in images if family_stem(m) == stem or family_stem(m).removesuffix('-flare') == stem]
        if want:
            hit = [m for m in family if image_tier(m) == want]
            if hit:
                note = None if hit[0].lower() == req.lower() else f"{req} → {hit[0]}"
                return hit[0], note
        if family and not want:
            ones = [m for m in family if image_tier(m) == "1k"]
            pick = ones[0] if ones else family[0]
            return pick, (None if pick.lower() == req.lower() else f"{req} → {pick}")
        die(f'当前 Key 没有 {req}' + (f' 的 {want.upper()} 档' if want else '') + '。没有自动替换为其他型号；用 models 查看这把 Key 的可用型号。')
    if want:
        hit = [m for m in images if image_tier(m) == want]
        if hit:
            return hit[0], None
    if not images:
        die('这把 Key 的实际目录没有图片模型，请检查所选 Key 的模型范围')
    if want:
        die(f'这把 Key 没有 {want.upper()} 图片档，没有擅自降低分辨率')
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
    "随时说“菜单”回到这里；说“视频菜单”或“图片菜单”看当前可用型号，也可以直接描述你想创作的画面。"
)


def collect_media_ids(ns: argparse.Namespace) -> tuple[list[str], str]:
    from live_menu import fetch_models
    LIVE_MODEL_TYPES.clear()
    result = fetch_models(menu_client(ns), ns)
    if not result.get('ok') or result.get('status') != 'ready':
        die(result.get('message', '请先连接土豆泥，再刷新菜单。'))
    LIVE_MODEL_TYPES.update({row['id'].casefold(): row['kind'] for row in result['models']})
    return [row['id'] for row in result['models']], 'live'


def menu_client(ns=None):
    pinned = getattr(ns, '_operation_auth', None)
    # A generation uses one credential snapshot for catalogue, submission and polling.
    # A new menu command has no pinned credentials and always sees the latest Key.
    return SimpleNamespace(resolve_key=(lambda _: pinned[0]) if pinned else resolve_key,
        resolve_base=(lambda _: pinned[1]) if pinned else resolve_base,
        http_json=http_json, load_catalog=load_catalog, classify_media=classify_media)


def load_lock() -> dict:
    if not LOCK_PATH.exists():
        return {"palette": [], "avoid": [], "refs": [], "last_output": None}
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def save_lock(data: dict) -> None:
    LOCK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_ledger(row: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if not (LEDGER_DIR/'.gitignore').exists():write_keybox_file(LEDGER_DIR/'.gitignore','*')
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
    timeout: int = 45,
    fail: bool = True,
) -> dict:
    _brand_lock()
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
        with urllib.request.build_opener(ApiRedirectHandler()).open(req, timeout=timeout) as resp:
            raw = resp.read(64 * 1024 * 1024 + 1)
            if len(raw)>64 * 1024 * 1024:
                raise ValueError('JSON response exceeds size limit')
            value=json.loads(raw.decode('utf-8-sig')) if raw else {}
            if not isinstance(value,dict):
                raise ValueError('API response is not a JSON object')
            return value
    except urllib.error.HTTPError as exc:
        detail = safe_error(exc.read(4096).decode("utf-8", errors="replace"),key)[:800]
        if not fail:
            return {"_http_error": exc.code, "_body": detail, "_url": url}
        if exc.code == 401 or "Invalid API Key" in detail:
            die(key_rejected_message(detail))
        die(f"HTTP {exc.code} {url}: {detail}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason=safe_error(str(getattr(exc,'reason',exc)),key)
        if not fail:
            return {"_http_error": 0, "_body": reason, "_url": url, '_transport_error':True}
        die(f'请求未完成：{reason}')
    return {}


def safe_error(text: str,key: str='') -> str:
    if key:text=text.replace(key,'[redacted]')
    return re.sub(r'sk-[A-Za-z0-9_-]{12,}','[redacted]',text)


class ApiRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        old,new=urlparse(req.full_url),urlparse(newurl)
        if (old.scheme,old.netloc)!=(new.scheme,new.netloc) or new.scheme!='https':
            raise urllib.error.HTTPError(req.full_url,code,'API redirect to another origin rejected',headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def validate_media_url(url: str) -> None:
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('媒体地址必须为 HTTPS 且不含账号信息')
    for record in socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(record[4][0]).is_global:
            raise ValueError('媒体下载不能访问本地或内网地址')


class MediaRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_media_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def http_download(url: str, dest: Path, key: str | None = None) -> Path:
    # Assets may be on the supplier's CDN. Only API requests carry the API Key; downloads never do.
    validate_media_url(url)
    dest=dest.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    req = urllib.request.Request(url, headers=headers)
    temporary=dest.with_name(dest.name+'.part-'+uuid.uuid4().hex[:8])
    last_error=None
    for attempt in range(3):
        try:
            with urllib.request.build_opener(MediaRedirectHandler()).open(req, timeout=180) as resp, temporary.open('wb') as fh:
                total=0
                while True:
                    chunk=resp.read(1024*1024)
                    if not chunk:break
                    total+=len(chunk)
                    if total>1024*1024*1024:raise ValueError('媒体超过1GiB，已保留临时文件，未覆盖结果')
                    fh.write(chunk)
            last_error=None;break
        except (OSError,urllib.error.URLError) as error:
            last_error=error
            # Some CDN TLS stacks reject Python's handshake. The installed OS curl uses its own verified TLS stack.
            # This is a credential-free GET; URL goes over stdin, no shell and no cross-host redirect following.
            executable=shutil.which('curl.exe') or shutil.which('curl')
            if executable:
                if any(c in url for c in '\r\n'):raise ValueError('Invalid media URL')
                config='url = "'+url.replace('\\','\\\\').replace('"','\\"')+'"\n'
                try:
                    result=subprocess.run([executable,'--config','-','--fail','--silent','--show-error','--proto','=https','--connect-timeout','20','--max-time','180','--max-filesize',str(1024*1024*1024),'--output',str(temporary)],input=config.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=190)
                    if result.returncode==0:last_error=None;break
                except subprocess.SubprocessError:pass
            if attempt<2:time.sleep(attempt+1)
    if last_error is not None:raise last_error
    with temporary.open('rb') as handle:prefix=handle.read(32)
    if not temporary.stat().st_size or prefix.lstrip().startswith((b'<',b'{')):
        raise ValueError('下载结果不是媒体文件，未覆盖结果')
    actual_suffix = media_suffix(prefix)
    if not actual_suffix:raise ValueError('下载内容不是可识别的图片或视频，未覆盖结果')
    dest=actual_media_path(dest,actual_suffix)
    os.replace(temporary,dest)
    return dest


def ratio_to_size(ratio: str) -> str:
    mapping = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}
    if ratio in ('16:9','9:16'):
        die('当前默认图片方案支持 1:1、3:2、2:3；不会把16:9伪装成3:2。请选支持的比例或指定已核实的 --size。')
    if ratio not in mapping:die('图片比例未核实，请使用 1:1、3:2、2:3 或已核实的 --size')
    return mapping[ratio]


def image_size_for(model: str,tier: str | None,ratio: str | None,explicit: str | None) -> str:
    schemas=json.loads((ROOT/'references'/'image-sizes.json').read_text('utf-8-sig')).get('models',{})
    schema=schemas.get(model,{})
    options=[str(value) for value in schema.get('options',[]) if re.fullmatch(r'\d{3,5}x\d{3,5}',str(value))]
    if explicit:
        if options and explicit not in options:die('所选图片尺寸不在该型号的已核实方案内')
        return explicit
    if not options:return ratio_to_size(ratio or '1:1')
    if not ratio and image_tier(model)!='other' and schema.get('default') in options:return schema['default']
    wanted=ratio or '1:1'
    if not re.fullmatch(r'\d{1,3}:\d{1,3}',wanted):die('画面比例格式无效')
    rw,rh=map(int,wanted.split(':'))
    if not rw or not rh:die('画面比例必须大于零')
    candidates=[]
    for option in options:
        w,h=map(int,option.split('x'))
        if abs(w/h-rw/rh)>0.015:continue
        if image_tier(model)=='other':
            actual='4k' if max(w,h)>=3500 else '2k' if max(w,h)>=1800 else '1k'
            if actual!=(tier or '1k'):continue
        candidates.append(option)
    if not candidates:die(f'{model} 的已核实尺寸中没有所选比例/分辨率组合；没有自动降档或改变比例')
    return schema.get('default') if schema.get('default') in candidates else candidates[0]


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
    cmd_menu(ns)


def cmd_menu(ns: argparse.Namespace) -> None:
    from live_menu import build_menu
    result = build_menu(menu_client(), ns)
    result['skill_version'] = (ROOT / 'VERSION').read_text('utf-8').strip()
    result['skill_update'] = {'status': 'not_checked', 'note': '模型菜单每次读取网站；插件版本由安装来源管理，这里不把目录刷新说成软件已升级。'}
    kind = getattr(ns, 'kind', 'all')
    result['selected_kind'] = kind
    image_count, video_count = len(result.get('images', [])), len(result.get('videos', []))
    selected = result.get('models', []) if kind == 'all' else result.get('images' if kind == 'image' else 'videos', [])
    requested = getattr(ns, 'model', None)
    if requested:
        selected = [row for row in selected if row['id'].casefold() == requested.casefold()]
    page, per_page = max(1, getattr(ns, 'page', 1)), min(30, max(1, getattr(ns, 'per_page', 12)))
    result['counts'] = {'images': image_count, 'videos': video_count, 'needs_adaptation': len(result.get('needs_adaptation', []))}
    result['pagination'] = {'page': page, 'per_page': per_page, 'total': len(selected), 'has_more': page * per_page < len(selected)}
    result['display_models'] = [{key: value for key, value in row.items() if requested or key != 'params'} for row in selected[(page-1)*per_page:page*per_page]]
    if result.get('status') == 'needs_connection':
        result['speak'] = ('你好，欢迎使用土豆泥图视。我可以帮你生成图片、制作视频，并把成品保存好。\n'
            '第一次使用，回复“连接”，只需在打开的窗口填写一次 Key。\n'
            '你也可以直接说“生图片”“生视频”“查看模型”或“我的作品”。\n' + INVOKE_MENU)
    elif result.get('status') == 'ready':
        lines = [f'已同步你的网站目录：图片 {image_count} 款，视频 {video_count} 款。']
        if kind != 'all' or requested:
            rows = result['display_models']
            lines.extend(f"{i}. " + (row['name'] if row['name']==row['id'] else f"{row['name']}（{row['id']}）") + (' · 需要适配' if not row['client_supported'] else '') for i, row in enumerate(rows, (page-1)*per_page+1))
            if not rows:
                lines.append('没有更多型号了，你可以说“菜单”返回首页。' if selected else '当前 Key 的网站目录没有这一项；不需要移动 Key 文件。')
            if result['pagination']['has_more']: lines.append('还有更多型号，你可以说“下一页”。')
        else:
            lines.append('你可以说“生图片”“生视频”“图片菜单”“视频菜单”“我的作品”或“继续任务”。')
        if result.get('warning'): lines.append(result['warning'])
        lines.append(INVOKE_MENU)
        result['speak'] = '\n'.join(lines)
    else:
        result['speak'] = result.get('message', '暂时无法刷新网站菜单。') + '\n你可以稍后说“刷新菜单”，或说“连接”检查连接。'
    # The full catalogue is used locally, but is not repeated three times into an
    # agent context. Details are requested for an explicit model when needed.
    for field in ('models', 'images', 'videos', 'needs_adaptation'):
        result.pop(field, None)
    emit(result)


def cmd_doctor(ns: argparse.Namespace) -> None:
    catalog = load_catalog()
    key = resolve_key(ns)
    base = resolve_base(ns)
    used = ""
    if not getattr(ns,'key_file',None):
        for path in keybox_paths():
            if read_keybox_file(path):
                used = str(path)
                break
    home_ok = KEY_FILE.is_file() and os.access(KEY_FILE,os.R_OK)
    local_ok = local_key_file().is_file() and os.access(local_key_file(),os.R_OK)
    if getattr(ns,'key_file',None):used=str(Path(ns.key_file).expanduser().resolve())
    payload = {
        "ok": True,
        "ready": False,
        "keybox": bool(key),
        "key": mask_key(key) if key else "",
        "base_url": base,
        "plaza": catalog.get("plaza") or "https://tudouni-api.com",
        "keybox_path": used or ('TUDOUNI_API_KEY' if key else str(local_key_file())),
        "keybox_home": home_ok,
        "keybox_project": local_ok,
    }
    if not key:
        payload['setup_required'] = True
        payload['next'] = 'setup'
        payload['speak'] = '由助手打开本机配置窗口。客户只需粘贴 Key 并点击保存，无需创建文件或使用终端。'
        emit(payload)
        return
    ids, source = collect_media_ids(ns)
    buckets = bucket_media(ids)
    payload["ready"] = source == "live" and bool(any(buckets['images'].values()) or buckets['videos'] or buckets['audio'])
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
    if not (ns.prompt or ns.subject):die('请用 --prompt 描述要生成的画面')
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
    size = image_size_for(model,getattr(ns,'tier',None),ns.ratio,ns.size)
    if not ns.ratio and re.fullmatch(r'\d{3,5}x\d{3,5}',size):
        width,height=map(int,size.split('x'));divisor=math.gcd(width,height);ns.ratio=f'{width//divisor}:{height//divisor}'
    spec = compose_spec(ns)
    prompt = dialect_prompt(spec)
    body = {"model": model, "prompt": prompt, "size": size, "n": 1, "response_format": "url"}
    out = output_path(ns.out,'image','.png')
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
    fingerprint=hashlib.sha256((base+'\n'+hashlib.sha256(key.encode()).hexdigest()+'\n'+json.dumps(body,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
    for old in saved_jobs():
        if old.get('fingerprint')==fingerprint and old.get('status') not in ('completed','failed','rejected') and not getattr(ns,'new_job',False):
            if old.get('url'):
                ns.job_id=old['job_id'];cmd_resume(ns);return
            die('同样的图片上次提交结果未确认；已保留 '+old['job_id']+'，不会自动再次收费。')
    job={'job_id':'image-'+uuid.uuid4().hex,'kind':'image','model':model,'status':'submitting','fingerprint':fingerprint,'out':str(out)};save_job(job)
    data = http_json("POST", f"{base}/v1/images/generations", key, body, timeout=600, fail=False)
    if data.get("_http_error") == 401:
        job['status']='rejected';job['reason']='auth_rejected';save_job(job)
        die(key_rejected_message(''))
    if '_http_error' in data:
        job['status']='submission_unknown' if data.get('_transport_error') or data.get('_http_error',0)>=500 else 'rejected';save_job(job)
        append_ledger({'cmd':'generate','model':model,'status':job['status'],'job_id':job['job_id'],'out':str(out)})
        die(('生成响应中断，结果未知；不要自动再提交。' if data.get('_transport_error') else '生成被拒绝；没有替换型号或重复提交。')+str(data.get('_body','')))
    item = ((data.get("data") or [{}])[0]) if isinstance(data.get("data"), list) else {}
    url = item.get("url")
    b64 = item.get("b64_json")
    saved = None
    job['status']='download_pending'
    if url:job['url']=url;save_job(job)
    if url:
        try:saved = str(http_download(url, out))
        except (OSError,ValueError) as error:
            die(f'图片已生成，下载未完成。用 resume --job-id {job["job_id"]} 继续下载，不要重新生图。'+safe_error(str(error),key))
    elif b64:
        import base64

        saved = str(save_inline_image(b64,out))
    else:
        die("image response missing url/b64_json")
    lock = load_lock()
    lock["last_output"] = saved
    save_lock(lock)
    append_ledger({"cmd": "generate", "model": model, "status": "completed", "out": saved, "mapped": mapped})
    job['status']='completed';job['out']=saved;job.pop('url',None);save_job(job)
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
    if not image_path.is_absolute():image_path=project_dir()/image_path
    if not image_path.exists():
        die(f"image not found: {image_path}")
    import base64

    image_bytes=image_path.read_bytes();suffix=media_suffix(image_bytes[:32]);mime={'.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp','.gif':'image/gif'}.get(suffix)
    if not mime:die('参考文件不是可识别的图片')
    payload = {
        "model": model,
        "prompt": prompt,
        "image": 'data:'+mime+';base64,' + base64.b64encode(image_bytes).decode("ascii"),
    }
    out = output_path(ns.out,'edit','.png')
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "url": f"{base}/v1/images/edits", "out": str(out), "image": str(image_path)})
        return
    data = http_json("POST", f"{base}/v1/images/edits", key, payload)
    item = ((data.get("data") or [{}])[0]) if isinstance(data.get("data"), list) else {}
    url = item.get("url")
    if not url and not item.get('b64_json'):die("edit response missing image")
    saved = str(http_download(url, out) if url else save_inline_image(item['b64_json'],out))
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


def submit_video(ns: argparse.Namespace) -> dict:
    if not (ns.prompt or ns.subject):die('请用 --prompt 描述要生成的视频')
    catalog = load_catalog()
    key, base = auth(ns)
    ids,source=collect_media_ids(ns)
    requested = next((mid for mid in ids if mid.casefold()==str(ns.model or '').casefold()), None)
    if requested and LIVE_MODEL_TYPES.get(requested.casefold()) == 'video' and classify_media(requested) != 'video':
        die('网站已开放这个新视频型号，但当前客户端执行路径需要适配；请在菜单中查看已适配型号。')
    videos=[mid for mid in ids if LIVE_MODEL_TYPES.get(mid.casefold(),classify_media(mid))=='video' and classify_media(mid)=='video']
    model=next((mid for mid in videos if mid.lower()==str(ns.model or '').lower()),None) if ns.model else next((mid for mid in videos if mid.lower()=='minimax-h3'),videos[0] if videos else None)
    if not model:die('当前 Key 没有请求的视频型号；没有提交任务。用 models 查看可用视频。')
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
    if row and row.get('parameter_style')=='size':
        sizes=row.get('sizes') or []
        choice=ns.size or {'16:9':'1280x720','9:16':'720x1280','1:1':'1024x1024','4:3':'960x720','3:4':'720x960','21:9':'1680x720'}.get(ns.ratio or '16:9')
        if choice not in sizes:die('该视频型号不支持所选尺寸；可用值：'+', '.join(sizes))
        if not 6<=seconds<=15:die('当前 MiniMax-H3 方案支持6到15秒，尚未提交')
        body.pop('ratio',None);body.pop('resolution',None);body['size']=choice;body['duration']=str(seconds)
    if ns.dry_run:
        return {"ok": True, "dry_run": True, "url": f"{base}/v1/video/generations", "body": body}
    fingerprint=hashlib.sha256((base+'\n'+hashlib.sha256(key.encode()).hexdigest()+'\n'+json.dumps(body,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
    for old in saved_jobs():
        if old.get('fingerprint')==fingerprint and old.get('status') not in ('completed','failed','rejected') and not getattr(ns,'new_job',False):
            if old.get('task_id'):return old
            die(f'同样的视频上次提交结果未确认，已保留 {old["job_id"]}，不会自动重复收费。')
    job={'job_id':'video-'+uuid.uuid4().hex,'kind':'video','model':model,'status':'submitting','fingerprint':fingerprint,'out':str(output_path(ns.out,'video','.mp4'))}
    save_job(job)
    data = http_json("POST", f"{base}/v1/video/generations", key, body,timeout=180,fail=False)
    if '_http_error' in data:
        job['status']='submission_unknown' if data.get('_transport_error') or data.get('_http_error',0)>=500 else 'rejected';save_job(job)
        if data.get('_http_error')==401:die(key_rejected_message(''))
        if data.get('_http_error')==403:die('当前 Key 没有这个视频型号的权限。请检查本站权限，不需要移动或重填 Key。')
        die(f'视频提交未确认（本地记录 {job["job_id"]}），未自动重发：'+str(data.get('_body','请求失败')))
    result=video_result(data);task_id=result['task_id']
    if not task_id:
        job['status']='submission_unknown';save_job(job);die(f'视频响应缺少任务编号，保留记录 {job["job_id"]}，不自动再次提交')
    job.update(task_id=task_id,status=result['status']);save_job(job)
    append_ledger(
        {
            "cmd": "submit-video",
            "model": model,
            "task_id": task_id,
            "status": job['status'],
            'job_id':job['job_id'],
        }
    )
    return job


def cmd_submit_video(ns: argparse.Namespace) -> None:
    job=submit_video(ns)
    emit({**{k:v for k,v in job.items() if k not in ('fingerprint','url')},'ok':True,'next':'video --task-id '+str(job.get('task_id',''))})


def save_job(job: dict) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(job.get('job_id',''))):raise ValueError('invalid local job ID')
    write_keybox_file(LEDGER_DIR/'jobs'/f'{job["job_id"]}.json',json.dumps(job,ensure_ascii=False,indent=2))
    if not (LEDGER_DIR/'.gitignore').exists():write_keybox_file(LEDGER_DIR/'.gitignore','*')


def saved_jobs() -> list[dict]:
    result=[]
    for path in (LEDGER_DIR/'jobs').glob('*.json'):
        try:result.append(json.loads(path.read_text('utf-8-sig')))
        except (OSError,ValueError):continue
    return result


def video_result(data: dict) -> dict:
    inner=data.get('data') if isinstance(data.get('data'),dict) else {}
    return {'task_id':data.get('task_id') or data.get('id') or inner.get('task_id') or inner.get('id'), 'status':normalize_status(data.get('status') or inner.get('status')), 'url':extract_url(data)}


def cmd_video(ns: argparse.Namespace) -> None:
    key,base=auth(ns)
    if ns.task_id:
        job=next((j for j in saved_jobs() if j.get('task_id')==ns.task_id),{'job_id':'video-'+uuid.uuid4().hex,'kind':'video','task_id':ns.task_id,'status':'in_progress','out':str((OUT_DIR/('video-'+uuid.uuid4().hex[:12]+'.mp4')).resolve())})
    else:job=submit_video(ns)
    if ns.dry_run:emit(job);return
    if ns.out:job['out']=str(output_path(ns.out,'video','.mp4'))
    deadline=time.monotonic()+max(1,min(ns.wait,3600));errors=0
    while time.monotonic()<deadline:
        data=poll_once(key,base,job['task_id']);result=video_result(data)
        if '_http_error' in data:
            if data['_http_error']==401:die(key_rejected_message('')+' 已有视频任务保留，连接完成后继续原任务。')
            if data['_http_error']==403:die('当前 Key 没有查询该视频的权限。任务已保留，请检查本站权限，不要重新提交视频或移动 Key 文件。')
            errors+=1
            if errors>=6:break
        else:
            errors=0;job['status']=result['status'];save_job(job)
            if result['status']=='failed':
                append_ledger({'cmd':'video','task_id':job['task_id'],'status':'failed'});die('视频任务未成功，任务号 '+job['task_id']+'；没有自动重新提交')
            if result['status']=='completed':
                if not result['url']:die('视频已完成但未返回下载地址；保留任务号 '+job['task_id']+'，稍后用同一编号恢复')
                job['status']='download_pending';job['url']=result['url'];save_job(job)
                try:saved=str(http_download(result['url'],Path(job['out'])))
                except (OSError,ValueError) as error:die('视频已完成，下载未完成；用 resume --job-id '+job['job_id']+' 继续。'+safe_error(str(error),key))
                job.update(status='completed',out=saved);job.pop('url',None);save_job(job)
                append_ledger({'cmd':'video','task_id':job['task_id'],'status':'completed','out':saved})
                emit({'ok':True,'status':'completed','task_id':job['task_id'],'job_id':job['job_id'],'out':saved,'path':saved});return
        print(json.dumps({'status':job['status'],'task_id':job['task_id'],'action':'waiting'},ensure_ascii=False),file=sys.stderr,flush=True)
        time.sleep(min(max(1,ns.interval),max(0,deadline-time.monotonic())))
    emit({'ok':True,'status':'pending','task_id':job['task_id'],'job_id':job['job_id'],'next':'video --task-id '+job['task_id'],'note':'任务已受理，只需继续查询；不要重复提交'})


def cmd_resume(ns: argparse.Namespace) -> None:
    job=next((j for j in saved_jobs() if j.get('job_id')==ns.job_id),None)
    if not job:die('未找到本地任务记录，请确认 --project-dir')
    if job.get('kind')=='video' and job.get('task_id'):
        ns.task_id=job['task_id'];ns.out=job.get('out');cmd_video(ns);return
    if job.get('url'):
        try:saved=str(http_download(job['url'],Path(job['out'])))
        except (OSError,ValueError) as error:die('下载仍未完成；原生成不会重复提交。'+str(error))
        job.update(status='completed',out=saved);job.pop('url',None);save_job(job);emit({'ok':True,'out':saved,'path':saved});return
    if job.get('task_id'):
        ns.task_id=job['task_id'];ns.out=job.get('out');cmd_video(ns);return
    if job.get('status')=='completed':emit({'ok':True,'out':job.get('out'),'path':job.get('out')});return
    die('该记录没有已确认的任务编号，不能自动重发收费请求')


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
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',str(task_id)):die('任务编号格式无效')
    task_id=quote(task_id,safe='')
    primary = f"{base}/v1/video/generations/{task_id}"
    data = http_json("GET", primary, key, None, fail=False)
    if '_http_error' not in data:
        return data
    if data.get("_http_error") in {404, 405}:
        fallback = http_json("GET", f"{base}/v1/tasks/{task_id}", key, None, fail=False)
        if '_http_error' not in fallback:
            return fallback
        return fallback
    return data


def cmd_poll(ns: argparse.Namespace) -> None:
    key, base = auth(ns)
    task_id = ns.task_id
    if not task_id:
        die("poll requires --task-id")
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "url": f"{base}/v1/video/generations/{task_id}"})
        return
    data = poll_once(key, base, task_id)
    if '_http_error' in data:die('查询暂未完成：'+str(data.get('_body','请求失败')))
    status = video_result(data)['status']
    append_ledger({"cmd": "poll", "task_id": task_id, "status": status})
    emit({"ok": True, "task_id": task_id, "status": status, "raw_status": data.get("status"), "data": data.get("data")})


def extract_url(data: dict, depth: int = 0) -> str | None:
    if not isinstance(data,dict) or depth>8:return None
    blob = data.get("data")
    if isinstance(blob, list) and blob:
        first = blob[0]
        if isinstance(first, dict):
            return first.get("url") or first.get("video_url")
        if isinstance(first, str):
            return first
    if isinstance(blob, dict):
        found=extract_url(blob,depth+1)
        if found:return found
    for name in ['url','video_url','download_url']:
        if isinstance(data.get(name),str):return data[name]
    for name in ['output','result','content']:
        nested=data.get(name)
        if isinstance(nested,dict):
            found=extract_url(nested,depth+1)
            if found:return found
        elif isinstance(nested,list):
            for item in nested:
                if isinstance(item,dict):
                    found=extract_url(item,depth+1)
                    if found:return found
    return None


def cmd_download(ns: argparse.Namespace) -> None:
    key, base = auth(ns)
    url = ns.url
    task_id = ns.task_id
    out = output_path(ns.out,'video','.mp4')
    if ns.dry_run:
        emit({"ok": True, "dry_run": True, "task_id": task_id, "out": str(out)})
        return
    if not url:
        if not task_id:
            die("download requires --url or --task-id")
        data = poll_once(key, base, task_id)
        if '_http_error' in data:die('查询未完成：'+str(data.get('_body','请求失败')))
        if video_result(data)['status'] != "completed":
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
        path=local_key_file()
        if path.exists():path.unlink()
        emit({"ok": True, "cleared": True, "path": str(path)})
        return
    if ns.set_key_file:
        src = Path(ns.set_key_file)
        if not src.exists():
            die("key file not found")
        dest = save_key(read_keybox_file(src))
        emit({"ok": True, "saved": True, "path": str(dest), "key": mask_key(read_keybox_file(dest))})
        return
    if ns.set_key_stdin:
        raw = sys.stdin.read()
        if not raw.strip():
            die("stdin empty")
        dest = save_key(raw)
        emit({"ok": True, "saved": True, "path": str(dest), "key": mask_key(read_keybox_file(dest))})
        return
    if ns.set_base:
        target=project_dir()/'.tudouni'/'base_url';write_keybox_file(target, assert_tudouni_origin(ns.set_base))
        emit({"ok": True, "base_url": resolve_base(ns), "path": str(target)})
        return
    key = resolve_key(ns)
    emit(
        {
            "ok": True,
            "path": str(local_key_file()),
            "key": mask_key(key) if key else "",
            "has_keybox": bool(resolve_key(ns)),
            "keybox_path": next((str(p) for p in keybox_paths() if read_keybox_file(p)), KEYBOX_HINT),
            "has_env": bool(os.environ.get("TUDOUNI_API_KEY")),
            "base_url": resolve_base(ns),
            "note": "已有凭据自动使用。更换 Key 由助手打开 setup --replace 配置窗口，不需要客户操作文件或终端。",
        }
    )


def cmd_setup(ns: argparse.Namespace) -> None:
    from onboarding import start_setup
    emit(start_setup(sys.modules[__name__], ns))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tudouni", description="tudouni-media CLI")
    p.add_argument("--base-url")
    p.add_argument('--project-dir',help='Customer working directory; do not change into the installed skill folder')
    p.add_argument('--key-file',help='Explicit path to a local Key file; never put the Key itself in arguments')
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
    sp = sub.add_parser('menu')
    sp.add_argument('--kind', choices=['all','image','video'], default='all')
    sp.add_argument('--page', type=int, default=1)
    sp.add_argument('--per-page', type=int, default=12)
    sp.add_argument('--model', help='Read the current parameters for one exact model')
    sp.add_argument('--refresh', action='store_true', help='Every menu request refreshes the website catalogue')
    sub.add_parser('welcome')
    sub.add_parser("doctor")
    sp = sub.add_parser('setup')
    sp.add_argument('--replace', action='store_true', help='Open the form to replace the current credential')
    sp.add_argument('--status', action='store_true', help='Assistant: inspect completion without displaying credentials')
    sp.add_argument('--wait', type=int, default=0, help='Assistant: wait up to 60 seconds for form completion')
    sp.add_argument('--no-open', action='store_true', help=argparse.SUPPRESS)
    sp = sub.add_parser("quote")
    sp.add_argument("--model", required=True)
    sp.add_argument("--duration", type=int)
    sp = sub.add_parser("generate")
    add_compose_flags(sp)
    sp.add_argument('--new-job',action='store_true')
    sp = sub.add_parser("edit")
    add_compose_flags(sp)
    sp.add_argument("--image")
    sp = sub.add_parser("submit-video")
    add_compose_flags(sp)
    sp.add_argument("--resolution")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument('--new-job',action='store_true')
    sp = sub.add_parser('video')
    add_compose_flags(sp)
    sp.add_argument('--resolution')
    sp.add_argument('--yes',action='store_true')
    sp.add_argument('--new-job',action='store_true')
    sp.add_argument('--task-id',help='Resume polling only; never posts a new video')
    sp.add_argument('--wait',type=int,default=900)
    sp.add_argument('--interval',type=int,default=8)
    sp=sub.add_parser('resume')
    sp.add_argument('--job-id',required=True)
    sp.add_argument('--wait',type=int,default=900)
    sp.add_argument('--interval',type=int,default=8)
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
    g = argparse.Namespace(dry_run=False, base_url=None, project_dir=None, key_file=None)
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
        elif a in ('--project-dir','--key-file') and i+1<len(argv):
            field=a[2:].replace('-','_')
            i += 1
            setattr(g,field,argv[i])
        elif a.startswith(('--project-dir=','--key-file=')):
            field,value=a.split('=',1);setattr(g,field[2:].replace('-','_'),value)
        elif a=='--api-key' or a.startswith('--api-key='):
            die('不接受命令行明文 Key；请由助手使用 setup 打开配置窗口。')
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
    for field in ('project_dir','key_file'):
        if getattr(g,field):setattr(ns,field,getattr(g,field))
    configure_workspace(ns.project_dir)
    _brand_lock()
    dispatch = {
        "compose": cmd_compose,
        "models": cmd_models,
        'menu': cmd_menu,
        'welcome': cmd_menu,
        "doctor": cmd_doctor,
        'setup':cmd_setup,
        "quote": cmd_quote,
        "generate": cmd_generate,
        "edit": cmd_edit,
        "submit-video": cmd_submit_video,
        'video':cmd_video,
        'resume':cmd_resume,
        "poll": cmd_poll,
        "download": cmd_download,
        "jobs": cmd_jobs,
        "lock": cmd_lock,
        "config": cmd_config,
    }
    try:dispatch[ns.cmd](ns)
    except (OSError,ValueError) as error:die('操作未完成：'+safe_error(str(error))+'。已有任务或下载记录保留，可使用 jobs 和 resume 接续。')


if __name__ == "__main__":
    main()
