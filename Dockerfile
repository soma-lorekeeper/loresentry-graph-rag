FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --uid 10001 appuser
USER appuser

# 이미지 태그(build-<run>-<attempt>). 로그의 service.version이 된다. 태그가 바뀌어도
# 의존성 설치 layer를 다시 만들지 않도록 마지막에 둔다
ARG APP_VERSION=unknown
ENV APP_VERSION=${APP_VERSION}

EXPOSE 8000

# 시작 스크립트가 EDOT distro(opentelemetry-instrument)로 uvicorn을 띄운다. Deployment에 command를 주면 이 스크립트를
# 건너뛰어 추적 엔진 없이 뜨므로, 명령을 바꿀 때는 args로 넘긴다.
# 접근 로그는 앱의 request received·request completed가 대신한다(ECS JSON)
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
