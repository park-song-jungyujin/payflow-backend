class GuardRejection(Exception):
    """게이트가 요청을 막을 때 던진다. status_code는 항상 403 아니면 409다."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
