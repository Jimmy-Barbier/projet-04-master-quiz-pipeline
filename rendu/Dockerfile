FROM public.ecr.aws/lambda/python:3.13

RUN dnf install -y tar xz dejavu-sans-fonts && \
    curl -sL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz \
      | tar -xJ -C /tmp && \
    mv /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/ && \
    rm -rf /tmp/ffmpeg-* && dnf clean all

RUN pip install pillow

COPY app.py ${LAMBDA_TASK_ROOT}
COPY assembler.py ${LAMBDA_TASK_ROOT}

CMD ["app.lambda_handler"]