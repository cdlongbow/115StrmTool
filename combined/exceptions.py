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