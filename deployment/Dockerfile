FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY robot_vllm ./robot_vllm
RUN pip install --no-cache-dir '.[serve]' && useradd --uid 10001 --create-home runtime \
    && mkdir -p /var/lib/robot-runtime /var/log/robot-runtime && chown -R runtime:runtime /var/lib/robot-runtime /var/log/robot-runtime
USER runtime
EXPOSE 8765
ENTRYPOINT ["robot-runtime", "serve", "--host", "0.0.0.0", "--state-dir", "/var/lib/robot-runtime", "--output", "/var/log/robot-runtime"]
