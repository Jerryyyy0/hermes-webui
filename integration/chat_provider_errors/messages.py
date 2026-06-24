"""Chinese user-visible copy for chat stream apperror events."""
from __future__ import annotations

CHAT_ERROR_ZH: dict[tuple[str, str | None], dict[str, str]] = {
    ('connection_error', 'unreachable'): {
        'label': '无法连接模型服务',
        'message': '无法连接到配置的模型服务，请检查出口网关配置。',
        'hint': '',
    },
    ('connection_error', 'connect_refused'): {
        'label': '连接被拒绝',
        'message': '目标地址拒绝连接，模型服务可能未在该地址运行。',
        'hint': '请确认服务已启动、端口正确',
    },
    ('connection_error', 'timeout'): {
        'label': '连接超时',
        'message': '模型服务未及时响应，请确认服务正在运行且 URL 正确。',
        'hint': '检查网络状况后重试，或适当增大 Provider 超时设置。',
    },
    ('connection_error', 'dns'): {
        'label': '域名解析失败',
        'message': '无法解析配置的主机名，请检查 Base URL。',
        'hint': '核对 URL 拼写，或在 Docker 中使用主机 IP 地址。',
    },
    ('connection_error', 'ssl'): {
        'label': 'SSL 证书错误',
        'message': '无法与模型服务建立安全连接。',
        'hint': '检查 HTTPS 证书配置，或确认 Base URL 使用了正确的协议（http/https）。',
    },
    ('auth_mismatch', None): {
        'label': '身份验证失败',
        'message': 'API Key 无效，或当前 Provider 不支持所选模型。',
        'hint': '',
    },
    ('rate_limit', None): {
        'label': '请求过于频繁',
        'message': '已触发模型服务的速率限制。',
        'hint': '请稍后重试，或切换到其他 Provider。',
    },
    ('quota_exhausted', None): {
        'label': '额度已用尽',
        'message': '账户额度或用量已耗尽。',
        'hint': '请充值、等待额度重置',
    },
    ('model_not_found', None): {
        'label': '未找到模型',
        'message': '当前 Provider 找不到所选模型。',
        'hint': '请在设置中核对模型 ID',
    },
    ('no_response', None): {
        'label': '模型无响应',
        'message': '模型服务未返回任何内容。',
        'hint': '可能是额度或限流导致静默失败，请检查 Provider 状态后重试。',
    },
    ('compression_exhausted', None): {
        'label': '上下文压缩失败',
        'message': '对话上下文过长，无法继续安全压缩。',
        'hint': '请新建会话，或缩小当前任务范围后重试。',
    },
    ('cancelled', None): {
        'label': '任务已取消',
        'message': '任务已取消。',
        'hint': '',
    },
    ('interrupted', None): {
        'label': '响应被中断',
        'message': '响应在完成前被中断。',
        'hint': '如非您主动取消，请重试。',
    },
    ('gateway_auth_error', None): {
        'label': 'Gateway 身份验证失败',
        'message': 'Gateway 认证失败，无法继续请求。',
        'hint': '请检查 Gateway 凭据与路由配置。',
    },
    ('error', None): {
        'label': '发生错误',
        'message': '模型服务返回错误，请稍后重试。',
        'hint': '',
    },
}

CANCELLED_TURN_HINT = '您已主动停止，并非系统出错。'


def cancelled_turn_hint(agent_name: str | None = None) -> str:
    return CANCELLED_TURN_HINT


def build_user_error_content(
    *,
    err_type: str,
    error_code: str | None = None,
    raw_message: str = '',
) -> dict[str, str]:
    """Return Chinese {label, message, hint} for user-visible apperror surfaces."""
    _type = str(err_type or 'error').strip() or 'error'
    _code = str(error_code).strip() if error_code else None
    entry = CHAT_ERROR_ZH.get((_type, _code))
    if entry is None:
        entry = CHAT_ERROR_ZH.get((_type, None))
    if entry is None:
        entry = CHAT_ERROR_ZH[('error', None)]
    label = entry['label']
    message = entry['message']
    hint = entry.get('hint', '')
    return {'label': label, 'message': message, 'hint': hint}


def provider_details_label_for_type(err_type: str) -> str:
    if err_type == 'cancelled':
        return '取消详情'
    if err_type == 'interrupted':
        return '中断详情'
    return '技术详情'
