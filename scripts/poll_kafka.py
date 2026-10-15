"""명시한 sandbox topic에서 실제 Kafka polling + fake 업무 처리를 실행한다."""

import argparse
import json
import signal
from threading import Event

from confluent_kafka import Consumer

from app.kafka.fake import FakeConsumerHandler, FakeProducer
from app.kafka.polling import PollingWorker, consumer_config
from app.kafka.runtime import validate_fake_subscription


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-servers", required=True)
    parser.add_argument("--topic", action="append", required=True)
    parser.add_argument("--group-id", default="graph-rag-fake-polling")
    args = parser.parse_args()
    # A fake handler commits records without applying business effects. Keep it
    # away from real Content topics and production consumer group offsets.
    try:
        validate_fake_subscription(tuple(args.topic), args.group_id)
    except ValueError as exc:
        parser.error(str(exc))
    stop = Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    producer = FakeProducer()
    try:
        consumer = Consumer(consumer_config(args.bootstrap_servers, args.group_id))
        worker = PollingWorker(
            consumer, tuple(args.topic), FakeConsumerHandler(producer)
        )
        worker.run(stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        print(json.dumps({"fake_published": producer.count}))


if __name__ == "__main__":
    main()
