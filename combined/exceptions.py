"""
自定义异常定义：统一管理 API 错误，中间件层自动转 HTTP 响应
"""


class ServiceError(Exception):
    """服务层通用错误，自动转为 500"""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code

    def __str__(self):
        return self.message


class ConfigError(ServiceError):
    """配置错误，表示缺少配置或配置无效"""

    def __init__(self, message: str):
        super().__init__(message, status_code=503)


class ClientNotReadyError(ServiceError):
    """客户端未就绪或未初始化"""

    def __init__(self, message: str = "115 客户端未就绪，请检查 Cookie 配置"):
        super().__init__(message, status_code=503)


class NotFoundError(ServiceError):
    """资源未找到"""

    def __init__(self, message: str = "资源未找到"):
        super().__init__(message, status_code=404)


class BadRequestError(ServiceError):
    """请求参数错误"""

    def __init__(self, message: str = "请求参数错误"):
        super().__init__(message, status_code=400)


def format_exception(exc: Exception, max_len: int = 200) -> str:
    """
    格式化异常为适合日志和用户展示的短文本

    智能提取 HTTP API 返回的错误码和原因（115 格式：code=... reason='...' message='...'），
    裁剪文件回溯栈，截断到 max_len 长度

    :param exc (Exception): 原始异常
    :param max_len (int): 最大长度，默认 200

    :return str: 格式化后的异常描述
    """
    parts = []
    args = list(exc.args) if exc.args else [str(exc)]

    for arg in args:
        text = str(arg)
        if not text or text == "":
            continue
        # 尝试提取 115 API 错误格式: code=xxx reason='...' message='...'
        import re
        code_m = re.search(r"code[=:]\s*(\d+)", text)
        reason_m = re.search(r"reason[=:]\s*['\"]?([^'\",}]+)", text)
        msg_m = re.search(r"message[=:]\s*['\"]?([^'\",}]+)", text)
        if code_m and (reason_m or msg_m):
            detail = f"HTTP {code_m.group(1)}"
            if reason_m:
                detail += f" {reason_m.group(1).strip()}"
            if msg_m:
                detail += f": {msg_m.group(1).strip()}"
            parts.append(detail)
        else:
            # 普通异常：取第一行，去掉 Python 文件路径信息
            line = text.split("\n")[0].strip()
            # 去掉 115 API 返回的完整响应体（JSON 格式）
            if line.startswith("{") or line.startswith("<"):
                continue
            parts.append(line)

    result = " | ".join(parts) if parts else str(exc.__class__.__name__)
    if len(result) > max_len:
        result = result[:max_len] + "..."
    return result