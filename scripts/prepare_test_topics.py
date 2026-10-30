"""고정 loopback 일회용 broker의 전체 Content 왕복 테스트 토픽 준비."""

from confluent_kafka.admin import AdminClient, NewTopic


def main():
    admin = AdminClient({"bootstrap.servers": "127.0.0.1:19092"})
    topics = (
        "content.project.changed.v1",
        "content.project.changed.v2",
        "content.graph-refresh.requested.v1",
        "graph-rag.graph-refresh.completed.v1",
        "content.project.changed.v1.graph-rag.dlq",
        "content.project.changed.v2.graph-rag.dlq",
        "content.graph-refresh.requested.v1.graph-rag.dlq",
        "graph-rag.graph-refresh.completed.v1.content.dlq",
    )
    existing = admin.list_topics(timeout=5).topics
    for future in admin.create_topics(
        [
            NewTopic(topic, 6 if topic in topics[:2] else 3, 1)
            for topic in topics
            if topic not in existing
        ]
    ).values():
        future.result(timeout=15)
    print("Disposable localhost test topics are ready.")


if __name__ == "__main__":
    main()
