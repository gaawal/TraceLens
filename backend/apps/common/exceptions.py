from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    response.data = {
        "code": getattr(exc, "default_code", "request_failed"),
        "message": "请求处理失败",
        "details": response.data,
    }
    return response
