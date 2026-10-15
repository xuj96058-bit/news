"""Safe analysis diagnostics without provider bodies or credentials."""

_RESPONSE_ERRORS = {
    "empty_content": "模型沒有回傳可分析的正文，請稍後重試。",
    "truncated": "模型輸出達到長度上限，分析結果不完整，請縮短新聞後重試。",
    "invalid_json": "模型回傳的內容不是有效 JSON，請重試。",
    "invalid_shape": "模型回傳的資料格式不正確，請重試。",
    "invalid_schema": "模型回傳的分析欄位不完整或格式不正確，請重試。",
}


class ModelResponseError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in _RESPONSE_ERRORS else "invalid_shape"
        super().__init__(_RESPONSE_ERRORS[self.reason])


class AnalyzerReloadRequired(RuntimeError):
    pass


class ProviderConfigurationError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Invalid provider configuration")


class ProviderAPIError(RuntimeError):
    def __init__(self, status_code=None, *, reason=None):
        self.status_code = status_code
        self.reason = reason if reason in {
            "provider_connection_error", "provider_protocol_error", "provider_stream_error",
            "provider_or_processing_error", "generation_timeout",
        } else "provider_or_processing_error"
        super().__init__("Provider request failed")


def provider_status_code(error):
    """OpenRouter can report a provider status in an HTTP-200 SSE error."""
    status = getattr(error, "status_code", None)
    if type(status) is int and 400 <= status <= 599:
        return status
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = detail.get("code")
            if type(code) is int and 400 <= code <= 599:
                return code
            if isinstance(code, str) and code in {str(number) for number in range(400, 600)}:
                return int(code)
    return None


def provider_failure_reason(error):
    """Categorize transport failures without persisting provider bodies or messages."""
    if isinstance(error, ProviderAPIError):
        return error.reason
    status = provider_status_code(error)
    if status is not None:
        return f"provider_http_{status}"
    names = {kind.__name__ for kind in type(error).__mro__}
    if names.intersection({"APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout", "TimeoutException"}):
        return "generation_timeout"
    if names.intersection({"APIConnectionError", "ConnectError", "ConnectionError", "ReadError", "WriteError"}):
        return "provider_connection_error"
    if names.intersection({"RemoteProtocolError", "LocalProtocolError", "ProtocolError"}):
        return "provider_protocol_error"
    if "APIError" in names:
        return "provider_stream_error"
    return "provider_or_processing_error"


def analysis_error_message(error):
    if isinstance(error, AnalyzerReloadRequired):
        return "網頁仍載入更新前的分析模組，請重新啟動 Streamlit，再重新整理頁面。"
    if isinstance(error, ModelResponseError):
        return _RESPONSE_ERRORS[error.reason]
    if isinstance(error, ProviderConfigurationError):
        return {
            "unknown_provider": "請選擇支援的 AI 服務商。",
            "invalid_model": "模型名稱與所選服務商不符，請確認模型 ID。",
            "missing_key": "請輸入所選服務商的 API key，或設定該服務商的環境變數。",
        }.get(error.reason, "AI 服務商設定不正確，請檢查設定。")
    status_messages = {
        401: "API key 驗證失敗，請確認 key 完整且仍有效。",
        402: "API 帳戶可用額度不足，請查看服務商的餘額。",
        403: "API 服務拒絕存取，請確認帳戶與模型的使用權限。",
        404: "找不到設定的 API 端點或模型，請確認模型設定。",
        429: "API 請求過於頻繁或受到用量限制，請稍後重試。",
    }
    status = provider_status_code(error)
    if isinstance(status, int) and status in status_messages:
        return status_messages[status]
    if isinstance(error, TypeError):
        return "程式處理資料時發生型別錯誤（TypeError），請重新啟動 Streamlit 後重試。這不是 API key 驗證結果。"
    return f"分析失敗（{type(error).__name__}）。請稍後重試；若持續發生，請提供此錯誤類型以便排查。"
