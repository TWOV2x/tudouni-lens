"""Fresh, credential-scoped customer menu. This module never submits a paid request."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit

MAX_MODELS = 10000
_TYPE_NAMES = {
    'image': {'图片', '图像', '生图', 'image', 'images', 'image generation', 'image_generation', 'text-to-image', 'image-to-image'},
    'video': {'视频', '视频增强', 'video', 'videos', 'video generation', 'video_generation', 'video enhancement', 'video_enhancement', 'text-to-video', 'image-to-video'},
    'audio': {'音频', '语音', '音乐', 'audio', 'speech', 'music', 'tts', 'asr'},
    'text': {'聊天', '文本', '文字', '向量', '排序', '视觉模型', '视觉理解', 'chat', 'text', 'llm', 'embedding', 'embeddings', 'rerank', 'reranking', 'vision'},
}
_POSTS = {'image': {'/v1/images/generations': 'generate', '/v1/images/edits': 'edit'},
          'video': {'/v1/video/generations': 'video'}}
_POLLS = {'/v1/video/generations/{task_id}', '/v1/video/generations/{id}', '/v1/tasks/{task_id}', '/v1/tasks/{id}'}


def _checked_at():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _kind(value):
    label = value.strip().casefold() if isinstance(value, str) else ''
    for kind, names in _TYPE_NAMES.items():
        if label in names:
            return kind
    return 'unknown'


def _revision(rows):
    # Directory identity contains public model metadata only, never a credential or credential hash.
    projection = sorted((row['id'], row['type_name']) for row in rows)
    return hashlib.sha256(json.dumps(projection, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _failure(status, message, checked_at=None):
    return {'ok': False, 'status': status, 'checked_at': checked_at or _checked_at(),
            'source': 'live_api', 'models': [], 'directory_revision': None, 'message': message}


def _http_failure(response, checked_at=None):
    code = response.get('_http_error')
    if code == 401:
        return _failure('invalid_key', 'Key 未通过验证，请通过连接窗口更新；尚未提交生成请求。', checked_at)
    if code == 403:
        return _failure('forbidden', '这把 Key 当前无权读取网站目录，请检查网站权限；无需移动或重新填写 Key。', checked_at)
    if code == 429:
        return _failure('unavailable', '网站目录暂时繁忙，请稍后刷新；没有改用安装包里的旧目录。', checked_at)
    return _failure('unavailable', '网站目录暂时无法读取，请稍后刷新；没有把连接失败显示为空货盘。', checked_at)


def _credentials(lens, ns):
    try:
        key = lens.resolve_key(ns)
        if not key:
            return None, {'ok': True, 'status': 'needs_connection', 'checked_at': _checked_at(),
                          'source': 'not_connected', 'models': [], 'directory_revision': None,
                          'message': '欢迎使用土豆泥图视。连接后读取这把 Key 在网站上的实际图像和视频目录。',
                          'connection': {'command': 'setup', 'label': '连接土豆泥'},
                          'menu_hint': '随时说“打开土豆泥菜单”即可返回；也可以直接说想生成的图片或视频。'}
        return (key, lens.resolve_base(ns).rstrip('/')), None
    except (OSError, ValueError, SystemExit):
        return None, _failure('credential_error', '本机连接配置暂时无法读取，请由助手打开连接窗口检查；无需手动创建或移动文件。')


def _read_models(lens, credentials):
    key, base = credentials
    checked = _checked_at()
    try:
        response = lens.http_json('GET', base + '/v1/models', key, timeout=15, fail=False)
    except (OSError, ValueError, TypeError, SystemExit):
        return _failure('unavailable', '网站目录读取未完成，请稍后刷新；尚未提交生成请求。', checked)
    if not isinstance(response, dict):
        return _failure('invalid_response', '网站目录响应格式异常，请稍后重试；未使用旧目录代替。', checked)
    if '_http_error' in response:
        return _http_failure(response, checked)
    raw = response.get('data')
    if response.get('error') or response.get('success') is False or not isinstance(raw, list) or len(raw) > MAX_MODELS:
        return _failure('invalid_response', '网站目录响应格式异常，请稍后重试；未使用旧目录代替。', checked)
    rows, seen = [], {}
    for entry in raw:
        if not isinstance(entry, dict):
            return _failure('invalid_response', '网站目录包含异常型号记录，请稍后刷新。', checked)
        model_id, type_name = entry.get('id'), entry.get('type_name')
        if (not isinstance(model_id, str) or not model_id or model_id != model_id.strip()
                or len(model_id) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in model_id)
                or (type_name is not None and not isinstance(type_name, str))):
            return _failure('invalid_response', '网站目录包含异常型号记录，请稍后刷新。', checked)
        type_name = type_name or ''
        if len(type_name) > 200:
            return _failure('invalid_response', '网站目录包含异常类别记录，请稍后刷新。', checked)
        row = {'id': model_id, 'model_id': model_id, 'type_name': type_name, 'kind': _kind(type_name)}
        identity = model_id.casefold()
        if identity in seen:
            if seen[identity] != row:
                return _failure('invalid_response', '网站目录包含冲突型号，请刷新后核对。', checked)
            continue
        seen[identity] = row
        rows.append(row)
    return {'ok': True, 'status': 'ready', 'checked_at': checked, 'source': 'live_api',
            'directory_revision': _revision(rows), 'models': rows, 'model_count': len(rows),
            'message': '已重新读取这把 Key 的网站目录。' if rows else '网站已确认：这把 Key 当前没有可用型号。'}


def fetch_models(lens, ns):
    """Return live ids/type_name/kind or an explicit failure; never use bundled model names.

    No Key means needs_connection and no HTTP request. Each call resolves the current
    Key anew. `models=[]` with ok=False is not an authoritative empty catalogue.
    """
    credentials, result = _credentials(lens, ns)
    return result if result is not None else _read_models(lens, credentials)


def _safe_params(value, depth=0):
    if depth > 8:
        raise ValueError('nested parameter schema')
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('invalid numeric parameter')
        if isinstance(value, str) and len(value) > 16000:
            raise ValueError('large parameter text')
        return value
    if isinstance(value, list) and len(value) <= 512:
        return [_safe_params(item, depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 128:
        if not all(isinstance(key, str) and len(key) <= 200 for key in value):
            raise ValueError('invalid parameter fields')
        return {key: _safe_params(item, depth + 1) for key, item in value.items()}
    raise ValueError('invalid parameter schema')


def _read_plans(lens, credentials, allowed):
    key, base = credentials
    try:
        response = lens.http_json('GET', base + '/v1/studio/plans', key, timeout=15, fail=False)
        if not isinstance(response, dict) or '_http_error' in response:
            raise ValueError('plans unavailable')
        payload = response.get('data') if isinstance(response.get('data'), dict) else response
        raw = payload.get('models')
        if not isinstance(raw, list) or len(raw) > MAX_MODELS:
            raise ValueError('invalid plans')
        by_id = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get('model_id'), str):
                raise ValueError('invalid plan row')
            model_id = item['model_id']
            if model_id not in allowed:
                continue  # Plans never expand the Key's authoritative /models scope.
            name = item.get('name') or model_id
            endpoint, poll = item.get('endpoint') or '', item.get('poll_endpoint') or ''
            if not all(isinstance(value, str) and len(value) <= 2000 for value in (name, endpoint, poll)):
                raise ValueError('invalid plan metadata')
            params = _safe_params(item.get('params', []))
            if not isinstance(params, (dict, list)):
                raise ValueError('invalid params')
            projection = {'name': name, 'params': params, 'endpoint': endpoint, 'poll_endpoint': poll}
            if model_id in by_id and by_id[model_id] != projection:
                raise ValueError('conflicting plans')
            by_id[model_id] = projection
        revision = payload.get('plans_revision')
        if revision is not None and (not isinstance(revision, str) or len(revision) > 512):
            raise ValueError('invalid plans revision')
        return by_id, {'status': 'ready', 'revision': revision, 'source': 'live_studio_plans'}
    except (OSError, ValueError, TypeError, SystemExit):
        return {}, {'status': 'unavailable', 'revision': None, 'source': 'live_studio_plans',
                    'message': '图视参数方案暂时无法读取，已保留最新网站型号目录；未把方案价当作客户实扣价。'}


def _relative_endpoint(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(value.startswith('/') and not value.startswith('//') and not parsed.scheme
                and not parsed.netloc and not parsed.query and not parsed.fragment
                and not any(char.isspace() for char in value))


def _compatibility(lens, row, plan, catalog):
    kind, model_id = row['kind'], row['id']
    reasons, actions = [], []
    if kind not in ('image', 'video'):
        reasons.append('网站未提供可确认的图视类别，等待适配；没有按型号名称猜类别。')
    held = {str(value).casefold() for value in catalog.get('hold', [])}
    if model_id.casefold() in held:
        reasons.append('网站已对这把 Key 开放，旧客户端仍有兼容性限制，需要适配。')
    endpoint = plan.get('endpoint', '')
    poll = plan.get('poll_endpoint', '')
    if endpoint:
        if not _relative_endpoint(endpoint) or endpoint not in _POSTS.get(kind, {}):
            reasons.append('网站声明的生成接口尚未由当前客户端适配。')
        else:
            actions = [_POSTS[kind][endpoint]]
    elif kind in ('image', 'video'):
        actions = ['generate' if kind == 'image' else 'video']
    if kind == 'video' and poll and (not _relative_endpoint(poll) or poll not in _POLLS):
        reasons.append('网站声明的任务查询接口尚未由当前客户端适配。')
    # This checks existing CLI execution capability only, never changes the website's authoritative kind.
    try:
        known = lens.classify_media(model_id) == kind and kind in ('image', 'video')
    except (ValueError, TypeError):
        known = False
    if kind in ('image', 'video') and not known:
        reasons.append('网站已开放这个新型号，但当前客户端的型号执行路径尚待适配。')
    supported = not reasons
    return {'client_supported': supported, 'support_status': 'supported' if supported else 'needs_adaptation',
            'supported_actions': actions if supported else [], 'adaptation_reasons': reasons,
            'compatibility_source': 'live_plan_and_existing_client' if endpoint else 'existing_client'}


def _actions(connected, images=False, videos=False):
    return [
        {'id': 'connect', 'label': '连接土豆泥', 'command': 'setup', 'enabled': True},
        {'id': 'image', 'label': '生图片', 'command': 'generate', 'enabled': connected and images},
        {'id': 'video', 'label': '生视频', 'command': 'video', 'enabled': connected and videos},
        {'id': 'models', 'label': '查看全部图视模型', 'command': 'models', 'enabled': connected},
        {'id': 'works', 'label': '我的作品', 'command': 'jobs', 'enabled': True},
        {'id': 'resume', 'label': '继续任务', 'command': 'resume', 'enabled': True},
        {'id': 'replace_key', 'label': '更换 Key', 'command': 'setup --replace', 'enabled': True},
        {'id': 'help', 'label': '帮助', 'command': 'help', 'enabled': True},
    ]


def build_menu(lens, ns):
    """Fetch a fresh per-Key catalogue plus optional plans and return a customer menu.

    Billing objects, internal plan ids, secrets and non-media model names never
    enter this projection. `client_supported` is protocol compatibility, not proof
    that a paid generation has succeeded. Both reads use the same resolved Key.
    """
    credentials, initial = _credentials(lens, ns)
    main = initial if initial is not None else _read_models(lens, credentials)
    result = {key: value for key, value in main.items() if key != 'models'}
    result.update(title='土豆泥图视', menu_hint='随时说“打开土豆泥菜单”即可返回；也可以直接描述要创作的图片或视频。',
                  models=[], images=[], videos=[], needs_adaptation=[],
                  actions=_actions(main.get('status') == 'ready'),
                  pricing_note='实际客户价格请以网站当前账号的模型广场与最终用量账单为准；参数方案不是实扣价格。')
    if main.get('status') != 'ready':
        result['plans'] = {'status': 'not_requested', 'revision': None}
        return result
    plans, plans_status = _read_plans(lens, credentials, {row['id'] for row in main['models']})
    catalog_valid = True
    try:
        catalog = lens.load_catalog()
        if not isinstance(catalog, dict):
            raise ValueError('invalid client catalogue')
    except (OSError, ValueError, TypeError, SystemExit):
        catalog, catalog_valid = {}, False
    excluded = {'audio': 0, 'text': 0}
    for original in main['models']:
        if original['kind'] in excluded:
            excluded[original['kind']] += 1
            continue
        plan = plans.get(original['id'], {})
        row = dict(original, name=plan.get('name', original['id']), params=plan.get('params', []),
                   endpoint=plan.get('endpoint', ''), poll_endpoint=plan.get('poll_endpoint', ''))
        row.update(_compatibility(lens, row, plan, catalog))
        if not catalog_valid:
            row.update(client_supported=False, support_status='needs_adaptation', supported_actions=[])
            row['adaptation_reasons'].append('本地客户端兼容性配置无法读取；网站型号仍保留，需要助手修复客户端。')
        result['models'].append(row)
        if row['kind'] == 'image':
            result['images'].append(row)
        elif row['kind'] == 'video':
            result['videos'].append(row)
        if not row['client_supported']:
            result['needs_adaptation'].append(row)
    result.update(plans=plans_status, excluded_counts=excluded, media_model_count=len(result['models']),
                  plans_revision=plans_status['revision'],
                  actions=_actions(True, any('generate' in row['supported_actions'] for row in result['images']),
                                   any('video' in row['supported_actions'] for row in result['videos'])))
    # A website name/parameter edit must invalidate the menu even when an older
    # server's plans_revision does not include that field. No Key participates.
    view = {'directory': main['directory_revision'], 'plans': plans_status,
            'models': result['models'], 'excluded_counts': excluded}
    result['revision'] = hashlib.sha256(json.dumps(view, ensure_ascii=False, sort_keys=True,
                                                  separators=(',', ':')).encode('utf-8')).hexdigest()
    if plans_status['status'] != 'ready':
        result['warning'] = plans_status['message']
    if not result['models']:
        result['message'] = '网站目录已读取，这把 Key 当前没有开放图像或视频型号。'
    return result
