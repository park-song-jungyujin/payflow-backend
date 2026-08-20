FROM python:3.12-slim

WORKDIR /app
COPY src/ src/

# 이 목록은 pyproject.toml의 [project.dependencies]와 손으로 동기화해야 한다.
# uv sync/pip install .을 쓰지 않는 건 의도된 것이다(배포 방식 변경은 별도 논의).
# 의존성을 추가/제거하면 이 줄과 pyproject.toml을 함께 고쳐라 — 안 그러면
# 컨테이너가 ModuleNotFoundError로 기동 실패한다(C1, 2026-08-19).
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" google-auth requests python-dotenv google-cloud-firestore google-cloud-tasks google-cloud-storage openpyxl python-ulid

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
